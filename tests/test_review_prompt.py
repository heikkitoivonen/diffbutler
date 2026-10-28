"""Headless tests for explicit review prompts and cancellation."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from tests.test_pr_lookup import LookupForge, config, install_forge
from tests.test_sync import ACCOUNT, PR, REF
from textual.widgets import DataTable, Input, Static

from diffbutler.config.settings import Reviewer, load_settings
from diffbutler.core.application import local_store
from diffbutler.core.pr_lookup import ReviewTarget
from diffbutler.core.sync import PullRequestRow
from diffbutler.domain.review import ReviewConflict
from diffbutler.forges.base import ForgeError
from diffbutler.ui.tui.dashboard import Dashboard
from diffbutler.ui.tui.review_prompt import ReviewPrompt

pytestmark = [pytest.mark.serial, pytest.mark.asyncio]


async def test_refresh_moves_to_ctrl_r_and_R_can_cancel(tmp_path: Path) -> None:
    path = config(tmp_path, ["acme/api"])
    app = Dashboard(path, load_settings(path), offline=True)
    refreshed = asyncio.Event()

    async def sync() -> None:
        refreshed.set()

    app.sync = sync
    async with app.run_test() as pilot:
        await pilot.press("R")
        assert isinstance(app.screen, ReviewPrompt)
        assert not refreshed.is_set()
        await pilot.press("escape")
        assert not isinstance(app.screen, ReviewPrompt)
        assert not app.review_tasks
        await pilot.press("ctrl+r")
        await asyncio.wait_for(refreshed.wait(), 2)


@pytest.mark.parametrize(
    ("value", "ambiguous", "hidden"),
    [
        (PR.url, False, False),
        ("1", False, True),
        ("#1", False, True),
        ("1", True, True),
        (PR.url, False, True),
    ],
)
async def test_prompt_reviews_exact_choice_with_current_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, ambiguous: bool, hidden: bool
) -> None:
    forge = LookupForge()
    first = replace(PR, author="ReAdEr", assignees=("READER",)) if hidden else PR
    forge.prs[REF] = first
    second = replace(
        first, ref=replace(REF, repository="second"), url=PR.url.replace("/api/", "/second/")
    )
    if ambiguous:
        forge.prs[second.ref] = second
    install_forge(monkeypatch, forge)
    path = config(tmp_path, [] if hidden and value == PR.url else ["acme/*"])
    settings = load_settings(path)
    settings.reviewers = [Reviewer(id="chosen", preset="codex", command=["unused"])]
    app = Dashboard(path, settings, offline=True)
    calls: list[tuple[object, ...]] = []
    requested = asyncio.Event()
    release = asyncio.Event()

    async def review(*args: object) -> None:
        selected = app.selected()
        assert selected is not None
        assert selected.row.tracked.pull_request.url == args[1]
        assert selected.row.tracked.explicit_review
        calls.append(args)
        requested.set()
        await release.wait()
        raise ReviewConflict("Synthetic review stopped")

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.review_configured", review)
    async with app.run_test(size=(120, 40)) as pilot:
        assert not app.rows
        app.current_reviewer = "chosen"
        # The prompt also works from history and an empty/filtered queue.
        app.query_one(Input).value = "does not match"
        await pilot.press("h", "R")
        prompt = app.screen
        assert isinstance(prompt, ReviewPrompt)
        await pilot.press(*value, "enter")
        if ambiguous:
            async with asyncio.timeout(5):
                while prompt.busy:
                    await pilot.pause()
            assert prompt.query_one(DataTable).row_count == 2
            assert not calls
            async with local_store(path) as (store, _):
                assert not await store.accounts()
            await pilot.press("down", "enter")
        try:
            await asyncio.wait_for(requested.wait(), 5)
            assert len(calls) == 1
            assert calls[0][1:4] == (
                second.url if ambiguous else PR.url,
                ACCOUNT.login,
                "chosen",
            )
            assert not isinstance(app.screen, ReviewPrompt)
            assert app.view == "active"
            assert app.selected() is not None
            await pilot.press("r")
            assert len(calls) == 1
        finally:
            release.set()
            await asyncio.gather(*app.review_tasks.values())
        assert app.selected() is not None
        assert "Synthetic review stopped" in str(app.query_one("#status", Static).content)


@pytest.mark.parametrize("failure", ["timeout", "validation"])
async def test_lookup_failure_stays_in_dialog_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    forge = LookupForge()
    install_forge(monkeypatch, forge)
    path = config(tmp_path, ["acme/api"])
    app = Dashboard(path, load_settings(path), offline=True)
    from diffbutler.core.application import follow_review_target_configured

    async def follow(path: Path | None, target: ReviewTarget) -> PullRequestRow:
        if failure == "timeout":
            await asyncio.Event().wait()
        raise ForgeError("PR is closed or merged; choose an open PR")

    async def review(*args: object) -> None:
        raise ReviewConflict("Synthetic review stopped")

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.review_configured", review)
    monkeypatch.setattr("diffbutler.ui.tui.review_prompt.follow_review_target_configured", follow)
    monkeypatch.setattr("diffbutler.ui.tui.review_prompt.LOOKUP_TIMEOUT", 0.2)
    async with app.run_test() as pilot:
        await pilot.press("R", "1", "enter")
        prompt = app.screen
        assert isinstance(prompt, ReviewPrompt)
        async with asyncio.timeout(5):
            while prompt.busy:
                await pilot.pause()
        message = str(prompt.query_one("#lookup-status", Static).content)
        assert ("timed out" if failure == "timeout" else "closed or merged") in message
        assert not prompt.query_one(Input).disabled
        assert prompt.query_one(Input).has_focus
        assert not app.review_tasks and not app.rows
        monkeypatch.setattr(
            "diffbutler.ui.tui.review_prompt.follow_review_target_configured",
            follow_review_target_configured,
        )
        monkeypatch.setattr("diffbutler.ui.tui.review_prompt.LOOKUP_TIMEOUT", 60)
        # Retry succeeds at adding the PR before the review is attempted.
        await pilot.press("enter")
        async with asyncio.timeout(5):
            while isinstance(app.screen, ReviewPrompt) or not app.rows:
                await pilot.pause()
        await asyncio.gather(*app.review_tasks.values())
        assert app.selected() is not None


async def test_no_match_and_error_allow_retry_then_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = LookupForge()
    install_forge(monkeypatch, forge)
    path = config(tmp_path, ["acme/api"])
    app = Dashboard(path, load_settings(path), offline=True)
    async with app.run_test() as pilot:
        await pilot.press("R")
        prompt = app.screen
        assert isinstance(prompt, ReviewPrompt)
        for value, message in (("invalid", "positive PR number"), ("99", "No accessible open")):
            prompt.query_one(Input).value = value
            await pilot.press("enter")
            async with asyncio.timeout(5):
                while prompt.busy:
                    await pilot.pause()
            assert message in str(prompt.query_one("#lookup-status", Static).content)
            assert not prompt.query_one(Input).disabled
        await pilot.press("escape")
        assert not app.review_tasks
        async with local_store(path) as (store, _):
            assert not await store.accounts()


async def test_escape_during_slow_lookup_never_starts_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def lookup(path: Path | None, value: str) -> tuple[ReviewTarget, ...]:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return (ReviewTarget(1, ACCOUNT, PR),)

    monkeypatch.setattr("diffbutler.ui.tui.review_prompt.review_targets_configured", lookup)
    path = config(tmp_path, ["acme/api"])
    app = Dashboard(path, load_settings(path), offline=True)
    async with app.run_test() as pilot:
        await pilot.press("R", "1", "enter")
        await asyncio.wait_for(entered.wait(), 2)
        await pilot.press("escape")
        await asyncio.wait_for(cancelled.wait(), 2)
        assert not isinstance(app.screen, ReviewPrompt)
        assert not app.review_tasks
        async with local_store(path) as (store, _):
            assert not await store.accounts()
