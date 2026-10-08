"""Offline acceptance: published evidence, overrides, history and dashboard actions."""

import asyncio
import logging
import sys
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from rich.text import Text
from sqlalchemy import func, select
from tests.test_github import adapter
from tests.test_sync import ACCOUNT, NOW, PR, REF, FakeForge
from textual.widgets import DataTable, Static

from diffbutler.config.settings import load_settings
from diffbutler.core.application import local_queue
from diffbutler.core.dashboard import (
    CycleDetail,
    DashboardRow,
    DashboardService,
    HistoryFilter,
    Poller,
    SortSpec,
    column_value,
    dashboard_rows,
    detail_configured,
    sort_rows,
)
from diffbutler.core.sync import DiffButler, PullRequestRow
from diffbutler.domain.dashboard import COLUMN_IDS
from diffbutler.domain.forge import Discovery, RequestedPR
from diffbutler.domain.review import ReviewConflict
from diffbutler.domain.sync import (
    CommentEvent,
    RequestEvent,
    ReviewActivity,
    ReviewEvent,
    TrackedPR,
    reconcile,
)
from diffbutler.store.base import SyncConflict
from diffbutler.store.dashboard import SQLiteDashboardStore
from diffbutler.store.database import Database
from diffbutler.store.models import AIRun, ObservedActivity
from diffbutler.store.queue import SQLiteQueueStore
from diffbutler.store.review import SQLiteReviewStore
from diffbutler.ui.tui.dashboard import Dashboard


@pytest_asyncio.fixture
async def ledger(
    tmp_path: Path,
) -> AsyncGenerator[tuple[Database, SQLiteQueueStore, FakeForge, Path]]:
    path = tmp_path / "dashboard.toml"
    path.write_text(f'[db]\nurl = "sqlite+aiosqlite:///{tmp_path}/dashboard.db"\n')
    database = Database(load_settings(path).db.url)
    await database.initialize()
    queue = SQLiteQueueStore(database)
    forge = FakeForge()
    await DiffButler(queue, forge).sync()
    try:
        yield database, queue, forge, path
    finally:
        await database.close()


@pytest.mark.serial
@pytest.mark.asyncio
async def test_k_resets_only_selected_pr_and_refresh_rediscovers_it(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
) -> None:
    database, queue, forge, path = ledger
    other = replace(PR, ref=replace(REF, number=2), url=PR.url[:-1] + "2")
    forge.prs[other.ref] = other
    forge.discovery = Discovery(
        (RequestedPR(PR, ("user:reader",)), RequestedPR(other, ("user:reader",))), True
    )
    await DiffButler(queue, forge).sync()
    app = Dashboard(path, load_settings(path), offline=True)
    async with app.run_test() as pilot:
        row = app.selected()
        assert row is not None
        selected_id = row.row.tracked.task_id
        selected_ref = row.row.tracked.pull_request.ref
        await pilot.press("k")
        async with asyncio.timeout(10):
            while app.resetting_tasks:
                await pilot.pause()
        await pilot.pause()
        assert len(app.rows) == 1 and str(selected_id) not in app.rows
        assert len((await queue.snapshot(ACCOUNT)).items) == 1
        cycles = await SQLiteDashboardStore(database).cycles()
        reset = next(cycle for cycle in cycles if cycle.task_id == selected_id)
        assert reset.completion_reason == "local_reset" and reset.cleanup_status == "succeeded"
        await pilot.press("h")
        assert str(selected_id) in app.history
        await pilot.press("k")  # History is read-only.
        assert await SQLiteDashboardStore(database).cycles() == cycles
        await pilot.press("h")
        await DiffButler(queue, forge).sync()
        await app.reload()
        assert len(app.rows) == 2
        fresh = next(
            row for row in app.rows.values() if row.row.tracked.pull_request.ref == selected_ref
        )
        assert fresh.row.tracked.task_id != selected_id
        assert fresh.row.tracked.state == "PENDING_AI_REVIEW" and fresh.run_status == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("sources", [("user:reader",), ("team:acme/reviewers",), ()])
@pytest.mark.parametrize("assigned", [False, True])
async def test_own_pr_visibility_and_config_override(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
    sources: tuple[str, ...],
    assigned: bool,
) -> None:
    _, queue, forge, path = ledger
    setting = "include_assigned_prs" if assigned else "include_own_prs"
    own = replace(PR, assignees=("someone", "ReAdEr")) if assigned else replace(PR, author="ReAdEr")
    forge.prs[REF] = own
    forge.discovery = Discovery((RequestedPR(own, sources),), True)
    service = DiffButler(queue, forge, monitor_repositories=("acme/api",))
    await service.sync()
    before = await queue.snapshot(ACCOUNT)
    for view in ("active", "requested", "following", "triage", "posted"):
        assert await dashboard_rows(path, view) == ()
    assert await local_queue(path, "active", None) == ()

    # Configuration applies to already-saved rows, without a remote refresh.
    path.write_text(path.read_text() + f"\n[ui]\n{setting} = true\n")
    assert len(await dashboard_rows(path)) == 1
    assert len(await local_queue(path, "active", None)) == 1
    assert len(await dashboard_rows(path, "requested")) == bool(sources)
    assert await queue.snapshot(ACCOUNT) == before

    path.write_text(path.read_text().replace(f"{setting} = true", f"{setting} = false"))
    assert await dashboard_rows(path) == ()
    # Search still permits finding an own PR; following alone does not bypass the filter.
    await service.follow_pr(REF)
    assert await dashboard_rows(path, "following") == ()


@pytest.mark.asyncio
async def test_assignment_refresh_and_independent_visibility_overrides(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
) -> None:
    _, queue, forge, path = ledger
    service = DiffButler(queue, forge)
    for assignees, visible in ((("other",), True), (("READER",), False), ((), True)):
        pr = replace(PR, assignees=assignees)
        forge.prs[REF] = pr
        forge.discovery = Discovery((RequestedPR(pr, ("user:reader",)),), True)
        await service.sync()
        assert bool(await dashboard_rows(path)) is visible

    pr = replace(PR, author="reader", assignees=("reader",))
    forge.prs[REF] = pr
    forge.discovery = Discovery((RequestedPR(pr, ("user:reader",)),), True)
    await service.sync()
    config = path.read_text()
    for own, assigned in ((True, False), (False, True), (True, True)):
        path.write_text(
            config + f"\n[ui]\ninclude_own_prs = {str(own).lower()}\n"
            f"include_assigned_prs = {str(assigned).lower()}\n"
        )
        assert bool(await dashboard_rows(path)) is (own and assigned)


@pytest.mark.serial
@pytest.mark.asyncio
@pytest.mark.parametrize("initial_failure", [False, True])
async def test_routine_refresh_notices_display_once_per_launch(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
    monkeypatch: pytest.MonkeyPatch,
    initial_failure: bool,
) -> None:
    from diffbutler.forges.base import ForgeError

    path = ledger[3]
    refreshing = "Refreshing GitHub; requests are paced to respect rate limits."
    fail = initial_failure
    observed: list[str] = []

    async def poll(_: Path | None) -> None:
        observed.append(str(app.query_one("#status", Static).content))
        if fail:
            raise ForgeError("Synthetic refresh failure")

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.poll_configured", poll)
    for _ in range(2):
        app = Dashboard(path, load_settings(path), offline=True)
        fail = initial_failure
        async with app.run_test():
            if fail:
                with pytest.raises(ForgeError):
                    await app.sync()
                assert "Synthetic refresh failure" in str(app.query_one("#status", Static).content)
                fail = False
            else:
                await app.sync()
            assert observed[-1] == refreshing
            if initial_failure:
                app.status("Review ready")
                await app.sync()
                assert observed[-1] == "Review ready"
            assert str(app.query_one("#status", Static).content) == "Forge refreshed"

            app.status("Review ready")
            await app.sync()
            assert observed[-1] == "Review ready"
            assert str(app.query_one("#status", Static).content) == "Review ready"
            fail = True
            for _ in range(2):
                app.status("Review ready")
                with pytest.raises(ForgeError):
                    await app.sync()
                assert "Synthetic refresh failure" in str(app.query_one("#status", Static).content)


@pytest.mark.serial
@pytest.mark.asyncio
async def test_sync_logs_core_quota_and_resets_between_refreshes(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from diffbutler.domain.forge import RateLimit
    from diffbutler.forges.diagnostics import observe_rate_limit

    quotas = [
        RateLimit("core", 4900, 5000, NOW),
        RateLimit("search", 12, 30, NOW),
        RateLimit("search", 30, 30, NOW),
        RateLimit("core", 4800, 5000, NOW),
    ]

    async def poll(_: Path | None) -> None:
        for quota in quotas:
            observe_rate_limit(quota)

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.poll_configured", poll)
    path = ledger[3]
    app = Dashboard(path, load_settings(path), offline=True)
    with caplog.at_level(logging.DEBUG, logger="diffbutler.ui.tui.dashboard"):
        async with app.run_test():
            await app.sync()
            quotas.clear()
            await app.sync()
    assert [
        record.message
        for record in caplog.records
        if record.message.startswith("Forge sync completed")
    ] == [
        "Forge sync completed limit=5000 remaining=4800",
        "Forge sync completed limit=None remaining=None",
    ]

    starts = [record for record in caplog.records if record.message == "Forge sync started"]
    assert len(starts) == 2
    assert all(record.levelno == logging.DEBUG for record in starts)


@pytest.mark.serial
@pytest.mark.asyncio
async def test_dashboard_sync_adapts_polling_to_authenticated_quota(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from diffbutler.config.settings import Forge
    from diffbutler.domain.forge import RateLimit
    from diffbutler.forges.diagnostics import RateLimitSource, observe_rate_limit, observe_request

    async def poll(_: Path | None) -> None:
        source = RateLimitSource("https://api.github.com", True, "reader")
        for _request in range(80):
            observe_request(source, "core")
        now = datetime.now(UTC)
        observe_rate_limit(
            RateLimit("core", 900, 5000, now + timedelta(seconds=3000)),
            source,
            observed_at=now.timestamp(),
        )

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.poll_configured", poll)
    path = ledger[3]
    settings = load_settings(path)
    settings.forges = [Forge(kind="github", base_url="https://api.github.com", token_env="UNUSED")]
    app = Dashboard(path, settings, offline=True)
    async with app.run_test():
        assert app.poller.interval == 60
        await app.sync()
        assert 740 < app.poller.interval <= 750
        assert 730 < app.poller.delay() <= 750


@pytest.mark.serial
@pytest.mark.asyncio
@pytest.mark.parametrize("quit_action", [False, True])
async def test_shutdown_waits_for_nonreview_worker_cleanup(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path], quit_action: bool
) -> None:
    _, _, _, path = ledger
    app = Dashboard(path, load_settings(path), offline=True)
    running = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    async def operation() -> None:
        try:
            running.set()
            await asyncio.Event().wait()
        finally:
            closing.set()
            await release.wait()
            closed.set()

    async with app.run_test(size=(120, 40)):
        app.start(operation())
        await asyncio.wait_for(running.wait(), timeout=5)
        if not quit_action:
            # Textual initiates cancellation before dispatching Unmount.
            app.workers.cancel_all()
            await asyncio.wait_for(closing.wait(), timeout=5)
        shutdown = asyncio.create_task(app.action_quit() if quit_action else app.on_unmount())
        try:
            await asyncio.wait_for(closing.wait(), timeout=5)
            assert not shutdown.done()
            assert not closed.is_set()
            release.set()
            await asyncio.wait_for(shutdown, timeout=5)
            assert closed.is_set()
            assert not list(app.workers)
        finally:
            release.set()
            await asyncio.gather(shutdown, return_exceptions=True)


def feedback(
    *, state: str = "COMMENTED", author: str = "reader", head: str = "head", id: str = "review:1"
) -> ReviewEvent:
    return ReviewEvent(id, NOW + timedelta(days=1), author, state, head, "Please handle the error.")


@pytest.mark.parametrize("state", ["COMMENTED", "CHANGES_REQUESTED"])
@pytest.mark.asyncio
async def test_published_feedback_is_retained_once_and_manual_override_survives_restart(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
    state: str,
) -> None:
    database, queue, forge, _ = ledger
    service = DiffButler(queue, forge)
    review = feedback(state=state)
    comment = CommentEvent(
        "inline:1", review.created_at, "reader", "Handle errors here", "head", review.id
    )
    forge.activity = ReviewActivity(ACCOUNT, reviews=(review,), comments=(comment,))
    await service.sync()
    current = (await queue.snapshot(ACCOUNT)).items[0]
    assert current.state == "PENDING_AUTHOR_CHANGES"
    assert current.notice == "Posted review detected · 1 comments"
    assert current.posted_at is not None
    assert current.posted_at.replace(tzinfo=UTC) == review.created_at
    assert current.task_id is not None
    dashboard = DashboardService(SQLiteDashboardStore(database), SQLiteReviewStore(database))
    await dashboard.continue_manual_review(current.task_id)
    await service.sync()
    await service.sync()
    saved = (await SQLiteQueueStore(database).snapshot(ACCOUNT)).items[0]
    assert saved.state == "PENDING_HUMAN_REVIEW" and saved.opened_at is not None
    detail = await dashboard.detail(current.task_id)
    assert len(detail.cycle.receipts) == 2
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ObservedActivity)) == 2
        assert await db.scalar(select(func.count()).select_from(AIRun)) == 0
    await dashboard.mark_waiting_for_author(current.task_id)
    await service.sync()
    assert (await queue.snapshot(ACCOUNT)).items[0].state == "PENDING_AUTHOR_CHANGES"


@pytest.mark.parametrize(
    "event",
    [feedback(author="other"), feedback(state="PENDING"), feedback(head="old")],
)
def test_ambiguous_feedback_does_not_imply_waiting(event: ReviewEvent) -> None:
    old = TrackedPR(PR, state="FINDINGS_TRIAGE", activity=ReviewActivity(ACCOUNT))
    result = reconcile(old, PR, ReviewActivity(ACCOUNT, reviews=(event,)), ())
    assert result.state == "FINDINGS_TRIAGE"


def test_new_head_request_approval_and_incomplete_refresh_precedence() -> None:
    old = TrackedPR(PR, state="FINDINGS_TRIAGE", activity=ReviewActivity(ACCOUNT))
    event = feedback()
    assert (
        reconcile(old, PR, ReviewActivity(ACCOUNT, reviews=(event,), complete=False), ()).state
        == old.state
    )
    assert reconcile(
        old, PR, ReviewActivity(ACCOUNT, reviews=(event,), complete=False), ()
    ).refresh_pending
    assert (
        reconcile(
            old, replace(PR, head_sha="new"), ReviewActivity(ACCOUNT, reviews=(event,)), ()
        ).state
        == "PENDING_AI_REVIEW"
    )
    request = RequestEvent("req:2", NOW + timedelta(days=2))
    assert (
        reconcile(old, PR, ReviewActivity(ACCOUNT, requests=(request,), reviews=(event,)), ()).state
        == "PENDING_AI_REVIEW"
    )
    assert (
        reconcile(old, PR, ReviewActivity(ACCOUNT, reviews=(feedback(state="APPROVED"),)), ()).state
        == "DONE"
    )
    assert (
        reconcile(
            old, replace(PR, state="closed"), ReviewActivity(ACCOUNT, complete=False), ()
        ).state
        == "DONE"
    )
    comment = CommentEvent("discussion:1", NOW, "reader", "A question")
    result = reconcile(old, PR, ReviewActivity(ACCOUNT, comments=(comment,)), ())
    assert result.state == old.state and "discussion" in result.notice
    posted = reconcile(old, PR, ReviewActivity(ACCOUNT, reviews=(event,)), ())
    pushed = reconcile(
        posted, replace(PR, head_sha="new"), ReviewActivity(ACCOUNT, reviews=(event,)), ()
    )
    assert pushed.notice == "New commits detected" and pushed.posted_at == event.created_at


@pytest.mark.asyncio
async def test_action_invalidates_inflight_refresh_and_consumes_older_feedback(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
) -> None:
    database, queue, _, _ = ledger
    snapshot = await queue.snapshot(ACCOUNT)
    old = snapshot.items[0]
    assert old.task_id is not None
    activity = ReviewActivity(ACCOUNT, reviews=(feedback(),))
    stale = reconcile(old, PR, activity, ())
    store = SQLiteDashboardStore(database)
    await store.choose_state(old.task_id, waiting=False)
    with pytest.raises(SyncConflict):
        await queue.apply(ACCOUNT, snapshot.version, (stale,))
    fresh = await queue.snapshot(ACCOUNT)
    result = reconcile(fresh.items[0], PR, activity, ())
    assert result.state == "PENDING_HUMAN_REVIEW"
    await queue.apply(ACCOUNT, fresh.version, (result,))
    assert (await queue.snapshot(ACCOUNT)).items[0].review_key == feedback().key


@pytest.mark.asyncio
async def test_history_preserves_each_cycle_and_filters_without_reactivation(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
) -> None:
    database, queue, forge, _ = ledger
    task = (await queue.snapshot(ACCOUNT)).items[0].task_id
    assert task is not None
    reviews = SQLiteReviewStore(database)
    await reviews.mark_approved(task)
    forge.prs[REF] = replace(PR, head_sha="new-head", title="New title")
    await DiffButler(queue, forge).sync()
    current = (await queue.snapshot(ACCOUNT)).items[0]
    assert current.cycle == 2 and current.task_id != task and current.state == "DONE_PLUS"
    dashboard = DashboardService(SQLiteDashboardStore(database), reviews)
    history = await dashboard.history(
        HistoryFilter(repository="acme/api", number=1, reason="local_approval")
    )
    assert len(history) == 1 and history[0].task_id == task
    assert history[0].pull_request.title == PR.title and history[0].completed_head_sha == "head"
    assert history[0].cleanup_status == "pending"
    assert not await dashboard.history(HistoryFilter(repository="elsewhere/repo"))
    assert not await dashboard.history(HistoryFilter(since=datetime.now(UTC) + timedelta(days=1)))
    assert len(await dashboard.history(HistoryFilter(completed=None))) == 2
    with pytest.raises(ReviewConflict):
        await dashboard.continue_manual_review(task)
    assert (await queue.snapshot(ACCOUNT)).items[0].task_id == current.task_id


@pytest.mark.asyncio
async def test_github_reads_paginated_reviews_and_both_comment_kinds() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        assert request.method == "GET"
        if path == "/user":
            return httpx.Response(200, json={"login": "reader"})
        if path.endswith("/events"):
            return httpx.Response(200, json=[])
        if path.endswith("/reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "user": {"login": "reader"},
                        "state": "COMMENTED",
                        "body": "Review body",
                        "submitted_at": "2026-09-02T00:00:00Z",
                        "commit_id": "head",
                    }
                ],
            )
        page = int(request.url.params["page"])
        inline = "/pulls/" in path
        return httpx.Response(
            200,
            headers={"Link": f'<https://api.github.com{path}?page=2>; rel="next"'}
            if page == 1
            else {},
            json=[
                {
                    "id": page,
                    "user": {"login": "reader"},
                    "body": f"Comment {page}",
                    "created_at": "2026-09-02T00:00:00Z",
                    **({"commit_id": "head", "pull_request_review_id": 1} if inline else {}),
                }
            ],
        )

    forge = adapter(handle, teams=False)
    try:
        activity = await forge.get_review_activity(REF)
        assert activity.complete and len(activity.comments) == 4
        assert len({comment.id for comment in activity.comments}) == 4
        assert activity.reviews[0].body == "Review body"
        assert (
            sum(comment.review_id == activity.reviews[0].id for comment in activity.comments) == 2
        )
        assert calls.count("/repos/acme/api/pulls/1/comments") == 2
        assert calls.count("/repos/acme/api/issues/1/comments") == 2
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_incomplete_comment_page_defers_feedback() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user":
            return httpx.Response(200, json={"login": "reader"})
        if path.endswith("/comments"):
            return httpx.Response(403, json={"message": "PRIVATE"})
        if path.endswith("/events"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "user": {"login": "reader"},
                    "state": "COMMENTED",
                    "submitted_at": "2026-09-02T00:00:00Z",
                    "commit_id": "head",
                }
            ],
        )

    forge = adapter(handle, teams=False)
    try:
        activity = await forge.get_review_activity(REF)
        assert not activity.complete and activity.reviews
        result = reconcile(TrackedPR(PR, state="FINDINGS_TRIAGE"), PR, activity, ())
        assert result.refresh_pending and result.state == "FINDINGS_TRIAGE"
        assert "PRIVATE" not in str(activity)
    finally:
        await forge.close()


def test_all_columns_sort_stably_and_missing_findings_stay_last() -> None:
    row = DashboardRow(row=PullRequestRow(account=ACCOUNT, tracked=TrackedPR(PR)), findings=2)
    another = row.model_copy(update={"findings": 10})
    missing = row.model_copy(update={"findings": None})
    for column in COLUMN_IDS:
        column_value(row, column)
        assert len(sort_rows((row, another), SortSpec(column=column))) == 2
    assert sort_rows((row, missing, another), SortSpec(column="findings")) == (
        another,
        row,
        missing,
    )
    assert sort_rows((row, missing, another), SortSpec(column="findings", descending=False)) == (
        row,
        another,
        missing,
    )


@pytest.mark.serial
@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform not in {"linux", "darwin"}, reason="Native cleanup requires host/boot identity"
)
async def test_dashboard_state_actions_history_and_browser_failure(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, queue, _, path = ledger
    opened: list[str] = []

    async def browser(url: str, command: list[str] | None = None) -> bool:
        opened.append(url)
        return False

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.open_browser", browser)
    app = Dashboard(path, load_settings(path), offline=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.query_one(DataTable[str]).row_count == 1
        assert [str(column.label) for column in app.query_one(DataTable[str]).columns.values()] == [
            "PR",
            "Title",
            "Author",
            "State",
        ]
        assert "Repository: acme/api" in str(app.query_one("#preview", Static).content)
        preview = str(app.query_one("#preview", Static).content)
        assert "AI Status: Not started" in preview
        assert "Findings: —" in preview
        assert "Age: " in preview
        await pilot.press("w")
        # These actions run in workers; an idle message queue does not mean
        # their database work and final dashboard reload have completed.
        await asyncio.wait_for(
            asyncio.gather(*(worker.wait() for worker in app.workers)), timeout=5
        )
        await pilot.pause()
        assert (await queue.snapshot(ACCOUNT)).items[0].state == "PENDING_AUTHOR_CHANGES"
        await pilot.press("o")
        await asyncio.wait_for(
            asyncio.gather(*(worker.wait() for worker in app.workers)), timeout=5
        )
        await pilot.pause()
        assert opened == [PR.url]
        assert (await queue.snapshot(ACCOUNT)).items[0].state == "PENDING_HUMAN_REVIEW"
        await pilot.press("s", "S")
        await pilot.pause()
        prefs = await SQLiteDashboardStore(database).preferences()
        assert prefs["sort_by"] == "number" and prefs["sort_desc"] == "false"
        await pilot.press("m")
        await asyncio.wait_for(
            asyncio.gather(*(worker.wait() for worker in app.workers)), timeout=5
        )
        await pilot.pause()
        assert app.query_one(DataTable[str]).row_count == 0
        await pilot.press("h")
        await pilot.pause()
        assert app.query_one(DataTable[str]).row_count == 1
        await pilot.press("space")
        await asyncio.wait_for(
            asyncio.gather(*(worker.wait() for worker in app.workers)), timeout=5
        )
        await pilot.pause()
        assert opened == [PR.url, PR.url]
        assert (await queue.snapshot(ACCOUNT)).items[0].state == "DONE"
        async with database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(AIRun)) == 0
        assert "Cleanup: succeeded" in str(app.query_one("#preview", Static).content)
    assert not await dashboard_rows(path)


@pytest.mark.asyncio
async def test_poller_retries_without_review_capability_and_can_be_cancelled() -> None:
    calls = 0
    finished = asyncio.Event()

    async def refresh() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("offline")
        finished.set()

    poller = Poller(refresh, 0.005)
    task = asyncio.create_task(poller.run())
    try:
        await asyncio.wait_for(finished.wait(), timeout=1)
        poller.paused = True
        previous = calls
        await asyncio.sleep(0.03)
        assert calls == previous
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.serial
@pytest.mark.asyncio
async def test_browse_screen_and_detail_survive_background_refresh(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from diffbutler.config.settings import Forge
    from diffbutler.ui.tui.browse import BrowseScreen
    from diffbutler.ui.tui.dashboard import DetailScreen

    database, _, forge, path = ledger
    with path.open("a") as stream:
        stream.write(
            '\n[[forges]]\nkind="github"\nbase_url="https://api.github.com"\ntoken_env="UNUSED"\n'
        )

    async def open_fake(_: Forge) -> FakeForge:
        return forge

    monkeypatch.setattr("diffbutler.core.application.open_forge", open_fake)
    app = Dashboard(path, load_settings(path), offline=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("b")
        await pilot.pause()
        assert isinstance(app.screen, BrowseScreen)
        await pilot.press("enter")
        await pilot.pause()
        await app.local_tick()
        assert app.screen.query_one(DataTable).row_count == 1
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert "AI Status: Not started" in app.screen.content
        assert "Findings: —" in app.screen.content
        assert "Age: " in app.screen.content
        await app.local_tick()
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 1
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(AIRun)) == 0


def test_sort_mixed_empty_and_nonempty_status_strings() -> None:
    pending = DashboardRow(row=PullRequestRow(account=ACCOUNT, tracked=TrackedPR(PR)))
    succeeded = pending.model_copy(update={"run_status": "succeeded"})
    assert sort_rows((pending, succeeded), SortSpec(column="ai_status")) == (succeeded, pending)


@pytest.mark.serial
@pytest.mark.asyncio
async def test_queue_fits_and_truncates_across_terminal_resizes(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
) -> None:
    _, queue, forge, path = ledger
    prs = tuple(
        replace(
            PR,
            ref=replace(REF, number=number),
            title="界" * 100 + f" {number}",
            author="abcdefghijklmnopqrst",
        )
        for number in range(1, 31)
    )
    forge.prs = {pr.ref: pr for pr in prs}
    forge.discovery = Discovery(tuple(RequestedPR(pr, ("user:reader",)) for pr in prs), True)
    await DiffButler(queue, forge).sync()
    app = Dashboard(path, load_settings(path), offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        table = app.query_one(DataTable[Text])
        for width in (80, 120, 79, 80):
            await pilot.resize_terminal(width, 24)
            await pilot.pause()
            preview = app.query_one("#preview", Static)
            panel = app.query_one("#preview-scroll")
            assert panel.size.height == 4
            assert not panel.show_horizontal_scrollbar
            assert panel.virtual_size.width <= panel.size.width
            assert preview.size.height > len(app.preview_content.splitlines())
            title = table.get_row_at(0)[1]
            author = table.get_row_at(0)[2]
            assert title.plain.endswith("…")
            assert author.plain == "abcdefghijklmno…"
            assert table.row_count == 30
            if width >= 80:
                assert table.virtual_size.width <= table.size.width
                assert table.scrollbar_size_vertical == table.scrollbar_size_horizontal == 0
                assert not table.horizontal_scrollbar.display
                assert not table.vertical_scrollbar.display
            else:
                assert table.show_horizontal_scrollbar
                assert table.show_vertical_scrollbar
            await pilot.press("ctrl+end")
            await pilot.pause()
            assert table.cursor_row == 29
            assert table.scroll_y > 0
        assert "界" * 100 in app.preview_content


@pytest.mark.asyncio
async def test_inflight_future_dated_feedback_cannot_undo_newer_manual_action(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, queue, forge, _ = ledger
    current = (await queue.snapshot(ACCOUNT)).items[0]
    assert current.task_id is not None
    task_id = current.task_id
    event = replace(feedback(), created_at=datetime.now(UTC) + timedelta(hours=1))
    calls = 0

    async def activity(_ref: object) -> ReviewActivity:
        nonlocal calls
        calls += 1
        if calls == 1:
            await SQLiteDashboardStore(database).choose_state(task_id, waiting=False)
        return ReviewActivity(ACCOUNT, reviews=(event,))

    monkeypatch.setattr(forge, "get_review_activity", activity)
    assert (await DiffButler(queue, forge).sync()).complete
    assert calls == 2
    assert (await queue.snapshot(ACCOUNT)).items[0].state == "PENDING_HUMAN_REVIEW"
    await DiffButler(queue, forge).sync()
    assert (await queue.snapshot(ACCOUNT)).items[0].state == "PENDING_HUMAN_REVIEW"


@pytest.mark.serial
@pytest.mark.asyncio
async def test_empty_filter_restores_preview_and_rebound_waiting_key(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
) -> None:
    from textual.widgets import Input

    _, queue, _, path = ledger
    settings = load_settings(path)
    settings.keys = {"mark_waiting_for_author": ["j"]}
    app = Dashboard(path, settings, offline=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one(Input).value = "no match here"
        await pilot.pause()
        assert "No PRs" in str(app.query_one("#preview", Static).content)
        app.query_one(Input).value = ""
        await pilot.pause()
        assert PR.title in str(app.query_one("#preview", Static).content)
        await pilot.press("w")
        await pilot.pause()
        assert (await queue.snapshot(ACCOUNT)).items[0].state == "PENDING_AI_REVIEW"
        await pilot.press("j")
        await pilot.pause()
        assert (await queue.snapshot(ACCOUNT)).items[0].state == "PENDING_AUTHOR_CHANGES"


@pytest.mark.serial
@pytest.mark.asyncio
async def test_monitoring_switch_persists_and_drives_sync(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from diffbutler.core.application import sync_configured
    from diffbutler.core.dashboard import preferences_configured
    from diffbutler.domain.forge import Discovery

    _, queue, forge, path = ledger
    with path.open("a") as stream:
        stream.write(
            '\n[[forges]]\nkind = "github"\nbase_url = "https://api.github.com"\n'
            'login = "reader"\nrepositories = ["acme/api"]\n'
        )
    forge.discovery = Discovery((), True)

    async def open_fake(_config: object) -> FakeForge:
        return forge

    monkeypatch.setattr("diffbutler.core.application.open_forge", open_fake)
    app = Dashboard(path, load_settings(path), offline=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("a")
        await asyncio.wait_for(
            asyncio.gather(*(worker.wait() for worker in app.workers)), timeout=5
        )
        await pilot.pause()
        assert (await preferences_configured(path))["monitor_all_prs"] == "true"
        assert "Monitoring: all open PRs" in app.sub_title
    await sync_configured(path)
    assert forge.cursors == [None, "2"]
    assert (await queue.snapshot(ACCOUNT)).items[0].sources == ()
    restarted = Dashboard(path, load_settings(path), offline=True)
    async with restarted.run_test(size=(120, 40)) as pilot:
        assert "Monitoring: all open PRs" in restarted.sub_title
        await pilot.press("a")
        await asyncio.wait_for(
            asyncio.gather(*(worker.wait() for worker in restarted.workers)), timeout=5
        )
        await pilot.pause()
        assert (await preferences_configured(path))["monitor_all_prs"] == "false"
        assert restarted.view == "active"
        assert restarted.query_one(DataTable).row_count == 1


@pytest.mark.serial
@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["local", "merged", "closed", "approval"])
async def test_posted_review_stays_visible_after_request_removal_until_completion(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path], completion: str
) -> None:
    database, queue, forge, path = ledger
    service = DiffButler(queue, forge)
    forge.activity = ReviewActivity(ACCOUNT, reviews=(feedback(),))
    await service.sync()
    forge.discovery = Discovery((), True)
    forge.prs[REF] = replace(PR, requested_users=())
    for _ in range(2):
        await service.sync()
    saved = (await queue.snapshot(ACCOUNT)).items[0]
    assert saved.following and saved.sources == ()
    assert saved.state == "PENDING_AUTHOR_CHANGES"
    assert saved.task_id is not None

    # A new dashboard uses persisted active state despite the empty request query.
    app = Dashboard(path, load_settings(path), offline=True)
    async with app.run_test() as pilot:
        assert app.view == "active"
        assert app.selected() is not None
        assert app.rows[str(saved.task_id)].row.tracked.state == "PENDING_AUTHOR_CHANGES"
        await pilot.press("h", "h")
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 1

        forge.prs[REF] = replace(forge.prs[REF], head_sha="updated-head")
        await service.sync()
        await app.reload()
        assert app.rows[str(saved.task_id)].row.tracked.state == "PENDING_AI_REVIEW"
        async with database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(AIRun)) == 0

        if completion == "local":
            await SQLiteReviewStore(database).mark_approved(saved.task_id)
        elif completion == "approval":
            forge.activity = ReviewActivity(
                ACCOUNT,
                reviews=(
                    replace(
                        feedback(state="APPROVED", head="updated-head", id="review:2"),
                        created_at=NOW + timedelta(days=2),
                    ),
                ),
            )
        else:
            forge.prs[REF] = replace(forge.prs[REF], state=completion)
        await service.sync()
        await app.reload()
        assert app.query_one(DataTable).row_count == 0
        assert (await queue.snapshot(ACCOUNT)).items[0].state == "DONE"


@pytest.mark.serial
@pytest.mark.asyncio
@pytest.mark.parametrize("sources", [(), ("assignee:reader",), ("user:someone-else",)])
async def test_default_personal_view_retains_followed_prs_without_review_requests(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path], sources: tuple[str, ...]
) -> None:
    _, queue, _, path = ledger
    snapshot = await queue.snapshot(ACCOUNT)
    item = replace(snapshot.items[0], sources=sources, pull_request=replace(PR, requested_users=()))
    await queue.apply(ACCOUNT, snapshot.version, (item,))
    app = Dashboard(path, load_settings(path), offline=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.view == "active"
        assert app.query_one(DataTable).row_count == 1
        await pilot.press("h", "h")
        await pilot.pause()
        assert app.view == "active"
        assert app.query_one(DataTable).row_count == 1
        await pilot.press("f")
        await pilot.pause()
        assert app.view == "requested"
        assert app.query_one(DataTable).row_count == 0
        await pilot.press("f")
        await pilot.pause()
        assert app.view == "following"
        assert app.query_one(DataTable).row_count == 1


@pytest.mark.asyncio
async def test_monitoring_requires_configured_repositories(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
) -> None:
    from diffbutler.core.dashboard import preferences_configured, toggle_monitoring_configured
    from diffbutler.forges.base import ForgeError

    path = ledger[3]
    with pytest.raises(ForgeError, match="Set repositories"):
        await toggle_monitoring_configured(path)
    assert "monitor_all_prs" not in await preferences_configured(path)


@pytest.mark.serial
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["config", "host", "session"])
async def test_review_configuration_failure_is_visible_and_logged(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from diffbutler.config.diagnostics import configure_logging, stop_logging
    from diffbutler.config.settings import ConfigError
    from diffbutler.reviewers.process import HostBootError

    _, queue, forge, path = ledger
    await DiffButler(queue, forge).sync()

    error = {
        "config": ConfigError("Native review requires a configured local repository path"),
        "host": HostBootError("Cannot verify this computer's host and boot identity"),
        "session": ReviewConflict("No agent conversation was created. Request another review"),
    }[failure]

    async def fail(*args: object) -> None:
        raise error

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.review_configured", fail)
    log = configure_logging()
    try:
        app = Dashboard(path, load_settings(path), offline=True)
        async with app.run_test() as pilot:
            await pilot.press("r")
            await pilot.pause()
            assert str(error) in str(app.query_one("#status", Static).content)
        content = log.read_text()
        assert "Review requested task=" in content
        assert "Dashboard operation failed" in content
        assert type(error).__name__ in content and "in fail" in content
    finally:
        stop_logging()


@pytest.mark.parametrize("column", COLUMN_IDS)
@pytest.mark.parametrize("descending", [False, True])
def test_post_approval_changes_sort_last_for_every_column_and_direction(
    column: str, descending: bool
) -> None:
    active = DashboardRow(row=PullRequestRow(account=ACCOUNT, tracked=TrackedPR(PR)))
    changed = DashboardRow(
        row=PullRequestRow(account=ACCOUNT, tracked=TrackedPR(PR, state="DONE_PLUS")),
        attention=1_000_000,
        findings=99,
    )
    another = changed.model_copy(update={"attention": 0, "findings": None})
    spec = SortSpec(column=column, descending=descending)
    ordered = sort_rows((changed, active, another), spec)
    assert ordered[0] == active
    assert ordered[1:] == sort_rows((changed, another), spec)
    assert column_value(changed, "state") == "done+"


@pytest.mark.parametrize("head_changed", [False, True])
@pytest.mark.parametrize(
    "state",
    [
        "PENDING_AI_REVIEW",
        "QUEUED",
        "AI_RUNNING",
        "PENDING_HUMAN_REVIEW",
        "FINDINGS_TRIAGE",
        "PENDING_AUTHOR_CHANGES",
        "DONE",
        "DONE_PLUS",
    ],
)
def test_renewed_request_takes_precedence_over_every_open_pr_state(
    state: str, head_changed: bool
) -> None:
    approval = ReviewEvent("approved", NOW, ACCOUNT.login, "APPROVED", PR.head_sha)
    previous = TrackedPR(
        PR,
        state=state,
        completion_reason="own_approval" if state == "DONE" else None,
        review_key=approval.key,
    )
    request = RequestEvent("requested-again", NOW + timedelta(seconds=1))
    pr = replace(PR, head_sha="new-head") if head_changed else PR
    result = reconcile(previous, pr, ReviewActivity(ACCOUNT, (request,), (approval,)), ())
    assert result.state == "PENDING_AI_REVIEW"
    assert result.notice == "Review requested again"
    assert result.cycle == (2 if state == "DONE" else 1)
    assert result.completion_reason is None


@pytest.mark.serial
@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["space", "r"])
@pytest.mark.parametrize("slow_read", ["preview", "refresh"])
async def test_start_second_review_while_dashboard_read_is_pending(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    slow_read: str,
) -> None:
    _, queue, forge, path = ledger
    second = replace(PR, ref=replace(REF, number=2), url=PR.url.replace("/1", "/2"))
    forge.prs[second.ref] = second
    forge.discovery = Discovery(
        tuple(RequestedPR(pr, ("user:reader",)) for pr in (PR, second)), True
    )
    await DiffButler(queue, forge).sync()
    reading = asyncio.Event()
    release_read = asyncio.Event()
    release_reviews = asyncio.Event()
    both_started = asyncio.Event()
    launched: list[str] = []

    async def review(
        path: Path | None,
        value: str,
        login: str | None,
        reviewer: str | None,
        progress: object,
    ) -> None:
        launched.append(value)
        if len(launched) == 2:
            both_started.set()
        await release_reviews.wait()
        raise ReviewConflict("Synthetic review stopped")

    async def detail(path: Path | None, task_id: UUID) -> CycleDetail:
        reading.set()
        await release_read.wait()
        return await detail_configured(path, task_id)

    async def rows(
        path: Path | None, view: str = "active", text: str = ""
    ) -> tuple[DashboardRow, ...]:
        reading.set()
        await release_read.wait()
        return await dashboard_rows(path, view, text)

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.review_configured", review)
    app = Dashboard(path, load_settings(path), offline=True)
    async with app.run_test() as pilot:
        await pilot.press(key)
        assert len(launched) == 1
        if slow_read == "preview":
            monkeypatch.setattr("diffbutler.ui.tui.dashboard.detail_configured", detail)
        else:
            monkeypatch.setattr("diffbutler.ui.tui.dashboard.dashboard_rows", rows)
            # Run through Textual's message pump, just like the periodic timer.
            app.call_later(app.local_tick)
        keys = asyncio.create_task(pilot.press("down", key))
        try:
            await asyncio.wait_for(reading.wait(), timeout=2)
            await asyncio.wait_for(both_started.wait(), timeout=2)
            assert set(launched) == {PR.url, second.url}
            assert not release_read.is_set()
            await keys
            # Repeated starts of this same PR must still be ignored.
            await pilot.press(key)
            assert len(launched) == 2
        finally:
            release_read.set()
            release_reviews.set()
            await keys
            await asyncio.gather(*app.review_tasks.values())


@pytest.mark.serial
@pytest.mark.asyncio
@pytest.mark.parametrize("preview_pane", [True, False])
async def test_cycle_reviewer_is_global_and_only_explicit_review_starts_it(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path],
    monkeypatch: pytest.MonkeyPatch,
    preview_pane: bool,
) -> None:
    _, queue, forge, path = ledger
    second = replace(PR, ref=replace(REF, number=2), url=PR.url.replace("/1", "/2"))
    forge.prs[second.ref] = second
    forge.discovery = Discovery(
        tuple(RequestedPR(pr, ("user:reader",)) for pr in (PR, second)), True
    )
    await DiffButler(queue, forge).sync()
    path.write_text(
        path.read_text()
        + "\n"
        + """
[[reviewers]]
id = "codex"
command = ["codex"]
preset = "codex"
[[reviewers]]
id = "legacy"
command = ["legacy"]
[[reviewers]]
id = "claude"
command = ["claude"]
preset = "claude"
[review]
default_reviewer = "codex"
[repos."acme/api"]
default_reviewer = "claude"
"""
    )
    original = path.read_text()
    launched: list[tuple[str, str | None]] = []
    resumed: list[DashboardRow] = []

    async def attach(self: Dashboard, row: DashboardRow) -> None:
        resumed.append(row)

    async def review(
        path: Path | None,
        value: str,
        login: str | None,
        reviewer: str | None,
        progress: object,
    ) -> None:
        launched.append((value, reviewer))
        raise ReviewConflict("Synthetic review stopped")

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.review_configured", review)
    monkeypatch.setattr(Dashboard, "attach", attach)
    settings = load_settings(path)
    settings.ui.preview_pane = preview_pane
    app = Dashboard(path, settings, offline=True)
    async with app.run_test() as pilot:

        def status_message() -> str:
            return str(app.query_one("#status", Static).content)

        assert "Current AI reviewer: codex - n to change" in status_message()
        assert not app.query("#reviewer")
        app.status("Review ready")
        await app.local_tick()
        assert status_message() == "Review ready"
        first = app.selected()
        assert first is not None
        await pilot.press("n")
        assert "Current AI reviewer: claude" in status_message()
        app.status("Another message")
        await app.local_tick()
        assert status_message() == "Another message"
        await pilot.press("c")
        await pilot.pause()
        assert [row.row.tracked.task_id for row in resumed] == [first.row.tracked.task_id]
        assert not launched and not app.review_tasks
        await pilot.press("down")
        await pilot.pause()
        assert app.current_reviewer == "claude"
        assert status_message() == "Another message"
        second_row = app.selected()
        assert second_row is not None
        assert second_row.row.tracked.task_id != first.row.tracked.task_id
        await pilot.press("r")
        await pilot.pause()
        assert launched == [(second_row.row.tracked.pull_request.url, "claude")]
        await pilot.press("up")
        await pilot.pause()
        assert app.current_reviewer == "claude"
        assert "Synthetic review stopped" in status_message()
        await pilot.press("n", "n")
        assert "Current AI reviewer: claude" in status_message()
        assert len(launched) == 1 and not app.review_tasks
        await pilot.press("space")
        await pilot.pause()
        assert launched[-1] == (first.row.tracked.pull_request.url, "claude")
        await pilot.press("h", "n")
        assert app.selected_key() is None
        assert "Current AI reviewer: codex" in status_message()
        await pilot.press("h")
        assert "Current AI reviewer: codex" in status_message()
        assert len(launched) == 2
    assert path.read_text() == original


@pytest.mark.serial
@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1])
async def test_cycle_reviewer_with_zero_or_one_native_reviewer(
    ledger: tuple[Database, SQLiteQueueStore, FakeForge, Path], count: int
) -> None:
    path = ledger[3]
    if count:
        path.write_text(
            path.read_text()
            + "\n"
            + """
[[reviewers]]
id = "codex"
command = ["codex"]
preset = "codex"
"""
        )
    app = Dashboard(path, load_settings(path), offline=True)
    async with app.run_test() as pilot:
        reviewer = "codex" if count else "not configured"
        assert f"Current AI reviewer: {reviewer}" in str(app.query_one("#status", Static).content)
        await pilot.press("n", "n")
        assert not app.review_tasks
        if count:
            assert app.current_reviewer == "codex"
            assert "Only codex is configured" in str(app.query_one("#status", Static).content)
        else:
            assert "No native reviewers configured" in str(app.query_one("#status", Static).content)
            assert app.current_reviewer is None
