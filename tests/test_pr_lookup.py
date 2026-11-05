"""Offline explicit-target resolution with multiple repositories and accounts."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from tests.test_sync import ACCOUNT, NOW, PR, REF, FakeForge

from diffbutler.config.settings import Forge
from diffbutler.core.application import (
    follow_review_target_configured,
    local_queue,
    local_store,
    review_targets_configured,
)
from diffbutler.core.dashboard import dashboard_rows
from diffbutler.core.pr_lookup import FOLLOW_UP_READS, numbered_prs
from diffbutler.core.sync import DiffButler
from diffbutler.domain.forge import Discovery, PRRef, PullRequest, ReadBudget
from diffbutler.domain.sync import ReviewEvent
from diffbutler.forges.base import ForgeError
from diffbutler.store.dashboard import SQLiteDashboardStore
from diffbutler.store.database import migration_config
from diffbutler.store.review import SQLiteReviewStore


class LookupForge(FakeForge):
    def ref(self, value: str) -> PRRef:
        if value.startswith("https://github.com/"):
            repository, number = value.removeprefix("https://github.com/").split("/pull/")
        elif "#" in value:
            repository, number = value.split("#")
        else:
            raise ForgeError("Invalid reference")
        owner, name = repository.split("/")
        return PRRef(self.id, owner, name, int(number))

    async def get_pull_request(self, ref: PRRef) -> PullRequest:
        if ref not in self.prs:
            raise ForgeError("Missing", status=404)
        return await super().get_pull_request(ref)


def config(tmp_path: Path, repositories: list[str]) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[db]\nurl="sqlite+aiosqlite:///{tmp_path}/lookup.db"\n'
        '[[forges]]\nkind="github"\nbase_url="https://api.github.com"\nlogin="reader"\n'
        f"repositories={repositories!r}\n"
    )
    return path


def install_forge(monkeypatch: pytest.MonkeyPatch, forge: LookupForge) -> None:
    async def open_fake(_: Forge) -> LookupForge:
        return forge

    monkeypatch.setattr("diffbutler.core.application.open_forge", open_fake)


@pytest.mark.asyncio
async def test_number_search_excludes_closed_merged_missing_and_deduplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = LookupForge()
    for repo, state in (("closed", "closed"), ("merged", "merged"), ("other", "open")):
        pr = replace(PR, ref=replace(REF, repository=repo), state=state)
        forge.prs[pr.ref] = pr
    install_forge(monkeypatch, forge)
    path = config(tmp_path, ["acme/api", "acme/missing", "acme/*", "acme/api"])
    targets = await review_targets_configured(path, " #1 ")
    assert {target.pull_request.ref.repository for target in targets} == {"api", "other"}
    assert not forge.cursors  # Number lookup does not depend on PR search coverage.
    assert forge.lookups.count(REF) == 1
    assert forge.closed
    async with local_store(path) as (store, _):
        assert await store.accounts() == ()  # Lookup never follows or creates review tasks.


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["1", PR.url])
async def test_unique_target_follow_rechecks_open_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    forge = LookupForge()
    install_forge(monkeypatch, forge)
    path = config(tmp_path, ["acme/api"])
    (target,) = await review_targets_configured(path, value)
    forge.prs[REF] = replace(PR, state="merged")
    with pytest.raises(ForgeError, match="closed or merged"):
        await follow_review_target_configured(path, target)
    async with local_store(path) as (store, _):
        assert not (await store.snapshot(ACCOUNT)).items
    forge.prs[REF] = PR
    row = await follow_review_target_configured(path, target)
    assert row.tracked.following and row.tracked.task_id is not None
    assert row.tracked.state == "PENDING_AI_REVIEW"
    assert not row.tracked.sources


@pytest.mark.asyncio
async def test_link_outside_scope_and_unknown_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = LookupForge()
    install_forge(monkeypatch, forge)
    path = config(tmp_path, [])
    assert len(await review_targets_configured(path, PR.url)) == 1
    with pytest.raises(ForgeError, match="No monitored repositories"):
        await review_targets_configured(path, "1")
    with pytest.raises(ForgeError, match="valid PR link"):
        await review_targets_configured(path, "https://unconfigured.example/a/b/pull/1")
    forge.prs[REF] = replace(PR, state="closed")
    assert not await review_targets_configured(path, PR.url)


@pytest.mark.asyncio
async def test_explicit_visibility_survives_refresh_restart_and_ends_at_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = LookupForge()
    forge.prs[REF] = replace(PR, author="ReAdEr", assignees=("READER",))
    forge.discovery = Discovery((), True)
    install_forge(monkeypatch, forge)
    path = config(tmp_path, [])
    (target,) = await review_targets_configured(path, PR.url)
    row = await follow_review_target_configured(path, target)
    assert row.tracked.explicit_review
    async with local_store(path) as (store, _):
        await DiffButler(store, forge).sync()
    # Each configured read opens a fresh database, as on dashboard restart.
    for view in ("active", "requested", "following", "triage", "posted"):
        (visible,) = await dashboard_rows(path, view, "unrelated filter")
        assert visible.row.tracked.task_id == row.tracked.task_id
        assert visible.row.tracked.explicit_review
        assert not visible.row.tracked.sources
    assert len(await local_queue(path, "active", None)) == 1
    assert row.tracked.task_id is not None
    async with local_store(path) as (store, _):
        await SQLiteReviewStore(store.database).mark_approved(row.tracked.task_id)
    assert not await dashboard_rows(path)
    row = await follow_review_target_configured(path, target)
    assert row.tracked.task_id is not None
    async with local_store(path) as (store, _):
        await SQLiteDashboardStore(store.database).reset_task(row.tracked.task_id)
        rediscovered = await DiffButler(store, forge).follow_pr(REF)
        assert not rediscovered.tracked.explicit_review
    assert not await dashboard_rows(path)


@pytest.mark.asyncio
async def test_visibility_migration_preserves_existing_monitors_without_overriding_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = LookupForge()
    forge.prs[REF] = replace(PR, author="reader", assignees=("reader",))
    install_forge(monkeypatch, forge)
    path = config(tmp_path, [])
    async with local_store(path) as (store, _):
        original = await DiffButler(store, forge).follow_pr(REF)
        async with store.database.engine.begin() as connection:
            await connection.run_sync(
                lambda conn: command.downgrade(migration_config(conn), "0010")
            )
    async with local_store(path) as (store, _):
        (migrated,) = (await store.snapshot(ACCOUNT)).items
        assert migrated.task_id == original.tracked.task_id
        assert migrated.pull_request == original.tracked.pull_request
        assert migrated.following and not migrated.explicit_review
    assert not await dashboard_rows(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "0", "-1", "hello", "http://github.com/a/b/pull/1"])
async def test_invalid_target(tmp_path: Path, value: str) -> None:
    with pytest.raises(ForgeError, match="link or a positive PR number"):
        await review_targets_configured(config(tmp_path, []), value)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["repositories", "rate_limited", "server", "wrong_owner"])
async def test_partial_results_never_appear_unambiguous(failure: str) -> None:
    class PartialForge(LookupForge):
        async def list_repositories(self, owner: str) -> tuple[str, ...]:
            if failure == "repositories":
                raise ForgeError("Repository listing failed", status=403)
            if failure == "wrong_owner":
                return ("other/api",)
            return ("acme/api", "acme/other")

        async def get_pull_request(self, ref: PRRef) -> PullRequest:
            if ref.repository == "other":
                if failure == "rate_limited":
                    raise ForgeError("Rate limited", status=403, retry_after=60)
                raise ForgeError("PR lookup failed", status=500)
            return await super().get_pull_request(ref)

    with pytest.raises(ForgeError):
        await numbered_prs(PartialForge(), ["acme/api", "acme/*"], 1)


class DeniedForge(LookupForge):
    """Lists acme/api and acme/blocked; PR reads in blocked are denied."""

    def __init__(self, status: int = 403) -> None:
        super().__init__()
        self.status = status
        self.budget = ReadBudget(None, 0.0)

    async def list_repositories(self, owner: str) -> tuple[str, ...]:
        return ("acme/api", "acme/blocked")

    def read_budget(self) -> ReadBudget:
        return self.budget

    async def get_pull_request(self, ref: PRRef) -> PullRequest:
        self.lookups.append(ref)
        if ref.repository == "blocked":
            raise ForgeError("Denied", status=self.status)
        return await super().get_pull_request(ref)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 451])
async def test_listed_repository_denying_pr_reads_is_skipped(status: int) -> None:
    assert await numbered_prs(DeniedForge(status), ["acme/*"], 1) == (PR,)


@pytest.mark.asyncio
async def test_exact_repository_denying_pr_reads_still_fails() -> None:
    with pytest.raises(ForgeError, match="Denied"):
        await numbered_prs(DeniedForge(), ["acme/*", "acme/blocked"], 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["quota", "deadline"])
async def test_listed_reads_beyond_budget_fail_before_any_pr_read(limit: str) -> None:
    forge = DeniedForge()
    deadline = None
    if limit == "quota":
        forge.budget = ReadBudget(1, 0.0)
        message = r"needs more requests than the forge's rate limit can spare \(1\)"
    else:
        forge.budget = ReadBudget(None, 20.0)
        deadline = asyncio.get_running_loop().time() + 30
        message = "would take about 240 seconds"
    with pytest.raises(ForgeError, match=message):
        await numbered_prs(forge, ["acme/*"], 1, deadline=deadline)
    assert not forge.lookups


@pytest.mark.asyncio
async def test_budget_spent_during_the_scan_stops_before_the_next_read() -> None:
    class RetryingForge(DeniedForge):
        async def get_pull_request(self, ref: PRRef) -> PullRequest:
            # Retries inside the first read consume quota the preflight counted on.
            self.budget = ReadBudget(FOLLOW_UP_READS, 0.0)
            return await super().get_pull_request(ref)

    forge = RetryingForge()
    forge.budget = ReadBudget(FOLLOW_UP_READS + 2, 0.0)
    with pytest.raises(ForgeError, match=r"in 1 more repositories .* can spare \(10\)"):
        await numbered_prs(forge, ["acme/*"], 1)
    assert {ref.repository for ref in forge.lookups} == {"api"}


@pytest.mark.asyncio
async def test_configured_lookup_applies_deadline_to_listed_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = DeniedForge()
    forge.budget = ReadBudget(None, 60.0)
    install_forge(monkeypatch, forge)
    path = config(tmp_path, ["acme/*"])
    deadline = asyncio.get_running_loop().time() + 30
    with pytest.raises(ForgeError, match="longer than the lookup allows"):
        await review_targets_configured(path, "1", deadline=deadline)
    assert len(await review_targets_configured(path, "1")) == 1


@pytest.mark.asyncio
async def test_exact_repositories_are_not_held_to_listing_budget() -> None:
    forge = DeniedForge()
    forge.budget = ReadBudget(0, 60.0)
    deadline = asyncio.get_running_loop().time() + 1
    assert await numbered_prs(forge, ["acme/api"], 1, deadline=deadline) == (PR,)


@pytest.mark.asyncio
async def test_multiple_configured_accounts_are_distinct_and_selection_revalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = LookupForge()
    second = LookupForge()
    second.account = replace(ACCOUNT, login="other")
    second.activity = replace(second.activity, account=second.account)
    path = config(tmp_path, ["acme/api"])
    with path.open("a") as stream:
        stream.write(
            '[[forges]]\nkind="github"\nbase_url="https://api.github.com"\n'
            'login="other"\nrepositories=["acme/api"]\n'
        )

    async def open_fake(config: Forge) -> LookupForge:
        return second if config.login == "other" else first

    monkeypatch.setattr("diffbutler.core.application.open_forge", open_fake)
    targets = await review_targets_configured(path, "1")
    assert [target.account.login for target in targets] == ["reader", "other"]
    row = await follow_review_target_configured(path, targets[1])
    assert row.account.login == "other"
    second.account = ACCOUNT
    with pytest.raises(ForgeError, match="account changed"):
        await follow_review_target_configured(path, targets[1])


@pytest.mark.asyncio
async def test_explicit_review_reopens_approved_open_pr_and_preserves_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = LookupForge()
    forge.activity = replace(
        forge.activity,
        reviews=(
            ReviewEvent(
                id="approved", author="reader", state="APPROVED", head_sha="head", created_at=NOW
            ),
        ),
    )
    install_forge(monkeypatch, forge)
    path = config(tmp_path, ["acme/api"])
    (target,) = await review_targets_configured(path, "1")
    async with local_store(path) as (store, _):
        completed = await DiffButler(store, forge).follow_pr(REF)
        assert completed.tracked.state == "DONE"
    row = await follow_review_target_configured(path, target)
    assert row.tracked.state == "PENDING_AI_REVIEW"
    assert row.tracked.task_id != completed.tracked.task_id
    assert row.tracked.cycle == completed.tracked.cycle + 1
    async with local_store(path) as (store, _):
        cycles = await SQLiteDashboardStore(store.database).cycles()
        assert len(cycles) == 2
        old = next(cycle for cycle in cycles if cycle.task_id == completed.tracked.task_id)
        assert old.completion_reason == "own_approval"
        assert old.cleanup_status == "pending"
        # The already-observed approval must not complete the new cycle again.
        refreshed = await DiffButler(store, forge).follow_pr(REF)
        assert refreshed.tracked.task_id == row.tracked.task_id
        assert refreshed.tracked.state == "PENDING_AI_REVIEW"
