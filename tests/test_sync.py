"""Offline queue acceptance with a programmable read-only forge and real SQLite."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from diffbutler.core.sync import DiffButler, list_queue
from diffbutler.domain.forge import (
    Account,
    Discovery,
    ForgeHealth,
    PRPage,
    PRQuery,
    PRRef,
    PullRequest,
    RequestedPR,
)
from diffbutler.domain.sync import RequestEvent, ReviewActivity, ReviewEvent
from diffbutler.forges.base import ForgeError
from diffbutler.store.base import SyncConflict
from diffbutler.store.database import Database
from diffbutler.store.models import CleanupJob, ReviewTask
from diffbutler.store.queue import SQLiteQueueStore

NOW = datetime(2026, 9, 1, tzinfo=UTC)
ACCOUNT = Account("github:https://api.github.com", "reader", "Reader")
REF = PRRef(ACCOUNT.forge, "acme", "api", 1)
PR = PullRequest(
    REF,
    "Synthetic PR",
    "author",
    "https://github.com/acme/api/pull/1",
    "open",
    "head",
    "base",
    "topic",
    "main",
    False,
    NOW,
    NOW,
    ("reader",),
    (),
)


class FakeForge:
    """No reviewer or publication capability exists on this fake."""

    def __init__(self) -> None:
        self.account = ACCOUNT
        self.id = ACCOUNT.forge
        self.prs = {REF: PR}
        self.discovery = Discovery((RequestedPR(PR, ("user:reader",)),), True)
        self.activity = ReviewActivity(ACCOUNT)
        self.lookups: list[PRRef] = []
        self.cursors: list[str | None] = []
        self.failed: set[PRRef] = set()
        self.closed = False

    def ref(self, value: str) -> PRRef:
        if value != "acme/api#1":
            raise ForgeError("Invalid synthetic reference")
        return REF

    async def whoami(self) -> Account:
        return self.account

    async def list_review_requests(self) -> Discovery:
        return self.discovery

    async def get_pull_request(self, ref: PRRef) -> PullRequest:
        self.lookups.append(ref)
        if ref in self.failed:
            raise ForgeError("Synthetic access failure")
        return self.prs[ref]

    async def get_review_activity(self, ref: PRRef) -> ReviewActivity:
        return self.activity

    async def search_pull_requests(self, query: PRQuery, cursor: str | None = None) -> PRPage:
        self.cursors.append(cursor)
        return PRPage(tuple(self.prs.values()), "2" if cursor is None else None)

    async def health(self) -> ForgeHealth:
        return ForgeHealth(self.account, (), None)

    async def close(self) -> None:
        self.closed = True


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncGenerator[Database]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/queue.db")
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_discovery_restart_removal_and_push(database: Database) -> None:
    forge = FakeForge()
    forge.discovery = Discovery(
        (RequestedPR(PR, ("user:reader",)), RequestedPR(PR, ("team:acme/reviewers",))), True
    )
    store = SQLiteQueueStore(database)
    service = DiffButler(store, forge)
    report = await service.sync()
    assert report.discovered == report.active == 1
    assert (await store.snapshot(ACCOUNT)).items[0].sources == (
        "team:acme/reviewers",
        "user:reader",
    )
    await database.close()
    reopened = Database(str(database.url))
    try:
        await reopened.initialize(auto_migrate=False)
        store = SQLiteQueueStore(reopened)
        service = DiffButler(store, forge)
        forge.discovery = Discovery((), True)
        forge.prs[REF] = replace(PR, head_sha="pushed", requested_users=())
        await service.sync()
        assert forge.lookups == [REF]
        rows = await list_queue(store)
        assert len(rows) == 1 and rows[0].tracked.following
        assert rows[0].tracked.pull_request.head_sha == "pushed"
        assert rows[0].tracked.state == "PENDING_AI_REVIEW"
        assert await list_queue(store, view="requested") == ()
        await service.sync()
        async with reopened.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(ReviewTask)) == 1
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_partial_discovery_and_failed_follow_refresh_preserve_queue(
    database: Database,
) -> None:
    forge = FakeForge()
    store = SQLiteQueueStore(database)
    service = DiffButler(store, forge)
    await service.sync()
    forge.discovery = Discovery((), False, ("Team visibility restricted",))
    forge.failed.add(REF)
    report = await service.sync()
    assert not report.complete and len(report.warnings) == 2
    item = (await store.snapshot(ACCOUNT)).items[0]
    assert item.sources == ("user:reader",) and item.state != "DONE"
    forge.failed.clear()
    forge.discovery = Discovery((), True)
    await service.sync()
    assert (await store.snapshot(ACCOUNT)).items[0].sources == ()


@pytest.mark.asyncio
async def test_browse_is_paged_and_has_no_persistence_side_effects(database: Database) -> None:
    forge = FakeForge()
    store = SQLiteQueueStore(database)
    service = DiffButler(store, forge)
    page = await service.browse_prs(PRQuery())
    assert page.complete and page.next_cursor == "2"
    assert (await service.browse_prs(PRQuery(), page.next_cursor)).next_cursor is None
    assert forge.cursors == [None, "2"]
    assert await store.accounts() == ()
    row = await service.follow_pr(REF)
    assert row.tracked.following and not row.tracked.sources
    assert row.tracked.task_id is not None
    await service.follow_pr(REF)
    assert len((await store.snapshot(ACCOUNT)).items) == 1


@pytest.mark.asyncio
async def test_approval_completion_suppression_new_cycles_and_cleanup(database: Database) -> None:
    forge = FakeForge()
    store = SQLiteQueueStore(database)
    service = DiffButler(store, forge)
    request = RequestEvent("request:1", NOW)
    other = ReviewEvent("review:1", NOW + timedelta(seconds=1), "other", "APPROVED", "head")
    forge.activity = ReviewActivity(ACCOUNT, (request,), (other,))
    await service.sync()
    assert len(await list_queue(store)) == 1
    own = replace(other, id="review:2", author="reader")
    forge.activity = replace(forge.activity, reviews=(other, own))
    await service.sync()
    assert await list_queue(store) == ()
    completed = (await store.snapshot(ACCOUNT)).items[0]
    assert completed.state == "DONE" and completed.completion_reason == "own_approval"
    await service.sync()
    await service.follow_pr(REF)
    assert await list_queue(store) == ()
    # A renewed same-SHA request is an event, not a request-membership toggle.
    renewed = RequestEvent("request:2", NOW + timedelta(seconds=2))
    forge.activity = replace(forge.activity, requests=(request, renewed, renewed))
    await service.sync()
    pending = (await store.snapshot(ACCOUNT)).items[0]
    assert pending.cycle == 2 and pending.state == "PENDING_AI_REVIEW"
    assert pending.task_id != completed.task_id
    await service.sync()
    assert (await store.snapshot(ACCOUNT)).items[0].cycle == 2
    forge.prs[REF] = replace(PR, state="merged")
    await service.sync()
    assert await list_queue(store) == ()
    forge.discovery = Discovery((), True)
    lookups = len(forge.lookups)
    await service.sync()
    assert len(forge.lookups) == lookups  # Closed PRs no longer poll.
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ReviewTask)) == 2
        jobs = (await session.scalars(select(CleanupJob))).all()
        assert len(jobs) == 2 and all(job.status == "pending" for job in jobs)
        old = await session.get(ReviewTask, completed.task_id)
        assert old is not None and old.completion_reason == "own_approval"


@pytest.mark.asyncio
async def test_partial_activity_does_not_consume_events_or_approve(database: Database) -> None:
    forge = FakeForge()
    store = SQLiteQueueStore(database)
    service = DiffButler(store, forge)
    own = ReviewEvent("review:1", NOW, "reader", "APPROVED", "head")
    forge.activity = ReviewActivity(
        ACCOUNT, reviews=(own,), complete=False, warnings=("Missing activity page",)
    )
    assert not (await service.sync()).complete
    assert (await store.snapshot(ACCOUNT)).items[0].review_key == ""
    assert len(await list_queue(store)) == 1
    forge.activity = replace(forge.activity, complete=True, warnings=())
    await service.sync()
    assert await list_queue(store) == ()
    forge.prs[REF] = replace(PR, head_sha="new-head")
    await service.sync()
    assert (await store.snapshot(ACCOUNT)).items[0].cycle == 2
    assert len(await list_queue(store)) == 1


@pytest.mark.asyncio
async def test_own_pr_visibility_is_per_reviewing_account(database: Database) -> None:
    forge = FakeForge()
    own = replace(PR, author="READER")
    forge.prs[REF] = own
    forge.discovery = Discovery((RequestedPR(own, ("team:acme/reviewers",)),), True)
    store = SQLiteQueueStore(database)
    await DiffButler(store, forge).sync()
    forge.account = replace(ACCOUNT, login="second")
    forge.activity = ReviewActivity(forge.account)
    await DiffButler(store, forge).sync()

    rows = await list_queue(store, view="requested")
    assert [row.account.login for row in rows] == ["second"]
    assert len(await list_queue(store, include_own_prs=True)) == 2
    assert await list_queue(store, login="reader") == ()


@pytest.mark.asyncio
async def test_account_isolation_expected_login_and_stale_writes(database: Database) -> None:
    forge = FakeForge()
    store = SQLiteQueueStore(database)
    service = DiffButler(store, forge, expected_login="wrong")
    with pytest.raises(ForgeError, match="identity"):
        await service.sync()
    assert await store.accounts() == ()
    await DiffButler(store, forge).sync()
    old = await store.snapshot(ACCOUNT)
    forge.account = replace(ACCOUNT, login="second")
    forge.activity = ReviewActivity(forge.account)
    forge.discovery = Discovery((), True)
    await DiffButler(store, forge).follow_pr(REF)
    assert len(await store.accounts()) == 2
    assert len(await list_queue(store, login="reader")) == 1
    assert (await store.snapshot(forge.account)).items[0].sources == ()
    await store.apply(ACCOUNT, old.version, old.items)
    with pytest.raises(SyncConflict):
        await store.apply(ACCOUNT, old.version, ())
    assert len((await store.snapshot(ACCOUNT)).items) == 1


@pytest.mark.asyncio
async def test_two_services_deduplicate_concurrent_sync(database: Database) -> None:
    store = SQLiteQueueStore(database)
    forge = FakeForge()
    await asyncio.gather(DiffButler(store, forge).sync(), DiffButler(store, forge).sync())
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ReviewTask)) == 1


@pytest.mark.asyncio
async def test_completion_and_cleanup_roll_back_together(database: Database) -> None:
    forge = FakeForge()
    store = SQLiteQueueStore(database)
    service = DiffButler(store, forge)
    await service.sync()
    snapshot = await store.snapshot(ACCOUNT)
    item = replace(snapshot.items[0], state="DONE", completion_reason="closed")
    foreign = replace(item, pull_request=replace(PR, ref=replace(REF, forge="other")))
    with pytest.raises(ValueError, match="different account forge"):
        await store.apply(ACCOUNT, snapshot.version, (item, foreign))
    assert len(await list_queue(store)) == 1
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(CleanupJob)) == 0


def test_cli_sync_browse_follow_and_offline_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from diffbutler.config.settings import Forge
    from diffbutler.forges.base import ReadForge
    from diffbutler.ui.cli import main

    config = tmp_path / "config.toml"
    config.write_text(
        '[[forges]]\nkind="github"\nbase_url="https://api.github.com"\n'
        'token_env="SYNTHETIC_TOKEN"\n'
    )
    forge = FakeForge()
    calls = 0

    async def open_fake(_config: Forge) -> ReadForge:
        nonlocal calls
        calls += 1
        return forge

    monkeypatch.setattr("diffbutler.core.application.open_forge", open_fake)
    args = ["--config", str(config)]
    main(["ls", *args, "--json"])
    assert capsys.readouterr().out.strip() == "[]" and calls == 0
    main(["ls", *args, "--view", "all", "--json"])
    assert '"next_cursor":"2"' in capsys.readouterr().out
    assert forge.closed
    main(["ls", *args, "--json"])
    assert capsys.readouterr().out.strip() == "[]" and calls == 1
    main(["sync", *args, "--json"])
    assert '"discovered": 1' in capsys.readouterr().out
    main(["follow", "acme/api#1", *args, "--json"])
    assert '"following":true' in capsys.readouterr().out
    previous_calls = calls
    main(["ls", *args])
    assert "acme/api#1" in capsys.readouterr().out and calls == previous_calls
    forge.discovery = Discovery((), False, ("restricted teams",))
    with pytest.raises(SystemExit) as error:
        main(["sync", *args])
    assert error.value.code == 1 and "restricted teams" in capsys.readouterr().err
    # Removing remote configuration still permits offline access to the local queue.
    config.write_text("")
    main(["ls", *args, "--json"])
    assert '"head_sha": "head"' in capsys.readouterr().out and calls == previous_calls + 1


@pytest.mark.asyncio
async def test_restart_preserves_completion_watermarks(database: Database) -> None:
    forge = FakeForge()
    forge.activity = ReviewActivity(
        ACCOUNT,
        (RequestEvent("1", NOW),),
        (ReviewEvent("1", NOW + timedelta(seconds=1), "reader", "APPROVED", "head"),),
    )
    store = SQLiteQueueStore(database)
    await DiffButler(store, forge).sync()
    await database.close()
    reopened = Database(str(database.url))
    try:
        await reopened.initialize()
        store = SQLiteQueueStore(reopened)
        service = DiffButler(store, forge)
        await service.sync()
        assert await list_queue(store) == ()
        forge.activity = replace(
            forge.activity,
            requests=(
                *forge.activity.requests,
                RequestEvent("2", NOW + timedelta(seconds=2)),
            ),
        )
        await service.sync()
        assert (await store.snapshot(ACCOUNT)).items[0].cycle == 2
        assert len(await list_queue(store)) == 1
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_failed_refresh_cannot_overwrite_other_accounts_newer_cache(
    database: Database,
) -> None:
    from diffbutler.store.models import PullRequest as StoredPR

    store = SQLiteQueueStore(database)
    first = FakeForge()
    await DiffButler(store, first).sync()
    second = FakeForge()
    second.account = replace(ACCOUNT, login="second")
    second.activity = ReviewActivity(second.account)
    second.prs[REF] = replace(PR, head_sha="newer-head")
    await DiffButler(store, second).follow_pr(REF)
    first.failed.add(REF)
    first.discovery = Discovery((), True)
    assert not (await DiffButler(store, first).sync()).complete
    async with database.sessions() as session:
        assert await session.scalar(select(StoredPR.head_sha)) == "newer-head"
    # The first account still sees the change on its next successful refresh.
    assert (await store.snapshot(ACCOUNT)).items[0].pull_request.head_sha == "head"
    first.failed.clear()
    first.prs[REF] = second.prs[REF]
    await DiffButler(store, first).sync()
    assert (await store.snapshot(ACCOUNT)).items[0].pull_request.head_sha == "newer-head"


@pytest.mark.asyncio
async def test_monitor_all_pages_without_request_membership(database: Database) -> None:
    forge = FakeForge()
    forge.discovery = Discovery((), True)
    forge.prs[REF] = replace(PR, requested_users=())
    store = SQLiteQueueStore(database)
    service = DiffButler(store, forge, monitor_repositories=("acme/api",))
    report = await service.sync()
    assert report.complete
    assert report.discovered == 1
    assert forge.cursors == [None, "2"]
    item = (await store.snapshot(ACCOUNT)).items[0]
    assert item.following
    assert item.sources == ()
    assert item.state == "PENDING_AI_REVIEW"
    assert await list_queue(store, view="requested") == ()
    # Turning off broad discovery retains existing monitors and reconciles closure.
    forge.prs[REF] = replace(forge.prs[REF], state="closed")
    await DiffButler(store, forge).sync()
    assert (await store.snapshot(ACCOUNT)).items[0].state == "DONE"


@pytest.mark.asyncio
async def test_monitor_search_failure_preserves_followed_prs(database: Database) -> None:
    class BrokenSearch(FakeForge):
        async def search_pull_requests(self, query: PRQuery, cursor: str | None = None) -> PRPage:
            raise ForgeError("Synthetic search failure")

    forge = BrokenSearch()
    store = SQLiteQueueStore(database)
    await DiffButler(store, forge).sync()
    report = await DiffButler(store, forge, monitor_repositories=("acme/api",)).sync()
    assert not report.complete
    assert report.refreshed == 1
    assert any("Synthetic search failure" in warning for warning in report.warnings)
    assert (await store.snapshot(ACCOUNT)).items[0].sources == ("user:reader",)
