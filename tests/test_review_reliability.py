"""Pending-review regressions use local Git, fake agents and synthetic forge activity."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from tests.test_native import configuration, prepared
from tests.test_review_claims import seed, service
from tests.test_review_results import captured
from tests.test_sources import git
from tests.test_sync import ACCOUNT, REF

from diffbutler.core.managed import (
    ManagedReviews,
    owned_directories,
    resolve_local,
    review_configured,
)
from diffbutler.core.native import NativeExecution
from diffbutler.core.review import ReviewService
from diffbutler.core.sync import DiffButler
from diffbutler.domain.lifecycle import CleanupStatus
from diffbutler.domain.review import RunRecord, RunStatus
from diffbutler.domain.sync import RequestEvent, ReviewActivity
from diffbutler.reviewers.base import ReviewContext, ReviewOutput
from diffbutler.store.database import Database
from diffbutler.store.queue import SQLiteQueueStore

pytestmark = pytest.mark.asyncio


@pytest.mark.serial
@pytest.mark.parametrize("outcome", ["complete", "cancel", "error"])
async def test_configured_review_is_durably_queued_before_slow_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    settings, forge = await prepared(tmp_path)
    config = configuration(tmp_path, settings)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "agent-home"))
    cleaning = asyncio.Event()
    release = asyncio.Event()
    original = ManagedReviews.cleanup

    async def cleanup(managed: ManagedReviews) -> CleanupStatus:
        cleaning.set()
        await release.wait()
        if outcome == "error":
            raise RuntimeError("Synthetic maintenance failure")
        return await original(managed)

    monkeypatch.setattr(ManagedReviews, "cleanup", cleanup)
    database = Database(settings.db.url)
    await database.initialize()
    try:
        queue = SQLiteQueueStore(database)
        await DiffButler(queue, forge).sync()
        managed = ManagedReviews(database, settings, await owned_directories(settings))
        task = asyncio.create_task(review_configured(config, "acme/api#1", None, None))
        try:
            await asyncio.wait_for(cleaning.wait(), timeout=5)
            row = (await queue.snapshot(ACCOUNT)).items[0]
            assert row.state == "QUEUED" and row.task_id is not None
            run = await managed.store.latest_run(row.task_id)
            assert run is not None and run.status == "queued"
            assert not await managed.leases.held_operations()
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == "error":
                release.set()
                with pytest.raises(RuntimeError, match="Synthetic maintenance failure"):
                    await task
            else:
                release.set()
                result = await asyncio.wait_for(task, timeout=15)
                assert result.id == run.id and result.status == "succeeded"
            if outcome != "complete":
                assert (await managed.reviews.get_run(run.id)).status == "cancelled"
                assert (await queue.snapshot(ACCOUNT)).items[0].state == "PENDING_AI_REVIEW"
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await database.close()


@pytest.mark.serial
async def test_busy_conversation_retains_identity_and_allows_explicit_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import sys

    settings, forge = await prepared(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "agent-home"))
    reviewer = settings.reviewers[0]
    original_command = reviewer.command
    agent = tmp_path / "busy.py"
    agent.write_text(
        "import sys\n"
        "assert 'resume' in sys.argv\n"
        "print('Error: thread/resume failed: thread\\nalready has an active writer '"
        "'(code -32600) private', file=sys.stderr)\n"
        "raise SystemExit(1)\n"
    )
    database = Database(settings.db.url)
    await database.initialize()
    try:
        queue = SQLiteQueueStore(database)
        await DiffButler(queue, forge).sync()
        managed = ManagedReviews(database, settings, await owned_directories(settings))
        row = await resolve_local(queue, "acme/api#1", None)
        first = await managed.review(row)
        session = await managed.reviews.get_session(first.session_id)
        reviewer.command = [sys.executable, str(agent)]
        failed = await managed.review(row)
        assert failed.status == RunStatus.FAILED
        assert failed.status_reason and "conversation is busy" in failed.status_reason
        assert failed.status_reason in caplog.text and "private" not in caplog.text
        assert failed.session_id == first.session_id
        assert not await managed.leases.held_operations()
        assert (await queue.snapshot(ACCOUNT)).items[0].state == "PENDING_AI_REVIEW"
        assert (await managed.reviews.get_session(first.session_id)) == session
    finally:
        await database.close()
    database = Database(settings.db.url)
    try:
        managed = ManagedReviews(database, settings, await owned_directories(settings))
        saved = await managed.reviews.get_run(failed.id)
        assert saved.status_reason == failed.status_reason
        result = await managed.reviews.get_result(failed.id)
        assert result is not None and result.stderr and b"active writer" in result.stderr
        latest = await managed.store.latest_run(first.task_id)
        assert latest is not None and latest.id == failed.id
        reviewer.command = original_command
        retry = await managed.review(row)
        assert retry.status == RunStatus.SUCCEEDED and retry.attempt == failed.attempt + 1
        assert retry.session_id == first.session_id
        assert (await managed.reviews.get_session(first.session_id)).backend_session_id == (
            session.backend_session_id
        )
    finally:
        await database.close()


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("new_request", [False, True])
async def test_request_history_after_partial_fetch(
    tmp_path: Path, completed: bool, new_request: bool
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/history.db")
    await database.initialize()
    try:
        forge, identity = await seed(database)
        queue = SQLiteQueueStore(database)
        forge.activity = ReviewActivity(ACCOUNT, complete=False)
        await DiffButler(queue, forge).sync()
        reviews = service(database)
        run = await reviews.request_review(identity)
        claim = await reviews.claim_review(run.id)
        assert claim is not None
        if completed:
            await reviews.finalize_review(claim.token, captured())
        # A request predating the explicit start is history, even if first seen now.
        event_time = run.requested_at.replace(tzinfo=UTC) + timedelta(
            seconds=1 if new_request else -1
        )
        forge.activity = ReviewActivity(ACCOUNT, requests=(RequestEvent("request", event_time),))
        await DiffButler(queue, forge).sync()
        row = (await queue.snapshot(ACCOUNT)).items[0]
        expected = (
            "PENDING_AI_REVIEW"
            if new_request
            else ("PENDING_HUMAN_REVIEW" if completed else "AI_RUNNING")
        )
        assert row.state == expected
        assert row.request_key == forge.activity.requests[0].key
        if not completed:
            assert (await reviews.get_run(run.id)).status == (
                RunStatus.STALE if new_request else RunStatus.RUNNING
            )
            await reviews.release_review(claim.token)
    finally:
        await database.close()


@pytest.mark.parametrize("comparison_changed", [False, True])
async def test_target_movement_checks_real_merge_base_during_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, comparison_changed: bool
) -> None:
    settings, forge = await prepared(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "agent-home"))
    repo = settings.repos["acme/api"].path
    assert repo is not None
    pr = forge.prs[REF]
    await git(repo, "checkout", "--", "changed.py")
    await git(repo, "checkout", "--detach", pr.base_sha)
    (repo / "unrelated.txt").write_text("another PR merged\n")
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "unrelated target change")
    target = (
        pr.head_sha
        if comparison_changed
        else (await git(repo, "rev-parse", "HEAD")).decode().strip()
    )
    database = Database(settings.db.url)
    await database.initialize()
    task = None
    try:
        queue = SQLiteQueueStore(database)
        await DiffButler(queue, forge).sync()
        managed = ManagedReviews(database, settings, await owned_directories(settings))
        started = asyncio.Event()
        proceed = asyncio.Event()
        checked = asyncio.Event()
        original_run = NativeExecution.run

        async def wait_for_target(
            self: NativeExecution,
            context: ReviewContext,
            session_id: str | None,
            checkpoint: Callable[[str], Awaitable[None]],
        ) -> ReviewOutput:
            started.set()
            await proceed.wait()
            return await original_run(self, context, session_id, checkpoint)

        monkeypatch.setattr(NativeExecution, "run", wait_for_target)
        row = await resolve_local(queue, "acme/api#1", None)
        task = asyncio.create_task(managed.review(row))
        await asyncio.wait_for(started.wait(), 10)
        original_current = ReviewService.claim_is_current

        async def observe(self: ReviewService, token: UUID) -> bool:
            result = await original_current(self, token)
            _, base = await self.store.comparison_snapshot(token)
            if base == target:
                checked.set()
            return result

        monkeypatch.setattr(ReviewService, "claim_is_current", observe)
        forge.prs[REF] = replace(pr, base_sha=target)
        await DiffButler(queue, forge).sync()
        await asyncio.wait_for(checked.wait(), 10)
        proceed.set()
        result = await asyncio.wait_for(task, 15)
        assert result.status == (RunStatus.STALE if comparison_changed else RunStatus.SUCCEEDED)
        assert (await queue.snapshot(ACCOUNT)).items[0].state == (
            "PENDING_AI_REVIEW" if comparison_changed else "FINDINGS_TRIAGE"
        )
        assert result.base_sha == pr.base_sha  # History retains the original pinned revisions.
        if comparison_changed:
            assert result.status_reason == "PR comparison changed during review"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await database.close()


async def test_finalization_rechecks_target_that_moves_during_comparison(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/race.db")
    await database.initialize()
    try:
        forge, identity = await seed(database)
        queue = SQLiteQueueStore(database)
        reviews = service(database)
        run = await reviews.request_review(identity)
        claim = await reviews.claim_review(run.id)
        assert claim is not None
        original = forge.prs[REF]
        forge.prs[REF] = replace(original, base_sha="first-target")
        await DiffButler(queue, forge).sync()
        seen: list[str] = []

        async def compare(_run: RunRecord, base: str) -> bool:
            seen.append(base)
            if base == "first-target":
                forge.prs[REF] = replace(original, base_sha="second-target")
                await DiffButler(queue, forge).sync()
                return True
            return False

        reviews.compare_base = compare
        result = await reviews.finalize_review(claim.token, captured())
        assert seen == ["first-target", "second-target"]
        assert result.status == RunStatus.STALE
        assert await reviews.get_result(run.id) is not None
    finally:
        await database.close()


@pytest.mark.parametrize("failure", ["timeout", "launch"])
async def test_failed_native_attempt_keeps_reason_and_available_output_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import sys

    from diffbutler.core.review import ReviewService
    from diffbutler.domain.review import ReviewPolicy
    from diffbutler.store.review import SQLiteReviewStore

    settings, forge = await prepared(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "agent-home"))
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import sys, time\n"
        "print('partial analysis', flush=True)\n"
        "print('partial diagnostic', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n"
    )
    reviewer = settings.reviewers[0]
    reviewer.command = (
        [sys.executable, str(agent)] if failure == "timeout" else [str(tmp_path / "missing-agent")]
    )
    reviewer.timeout_s = 3
    database = Database(settings.db.url)
    await database.initialize()
    run_id = None
    try:
        queue = SQLiteQueueStore(database)
        await DiffButler(queue, forge).sync()
        managed = ManagedReviews(database, settings, await owned_directories(settings))
        row = await resolve_local(queue, "acme/api#1", None)
        with pytest.raises(TimeoutError if failure == "timeout" else FileNotFoundError):
            await managed.review(row)
        assert row.tracked.task_id is not None
        run = await managed.store.latest_run(row.tracked.task_id)
        assert run is not None and run.status == RunStatus.FAILED
        run_id = run.id
        assert (await queue.snapshot(ACCOUNT)).items[0].state == "PENDING_AI_REVIEW"
        assert not await managed.leases.held_operations()
    finally:
        await database.close()
    database = Database(settings.db.url)
    try:
        assert run_id is not None
        reviews = ReviewService(SQLiteReviewStore(database), ReviewPolicy(frozenset()))
        saved = await reviews.get_run(run_id)
        result = await reviews.get_result(run_id)
        assert result is not None and result.error == saved.status_reason
        if failure == "timeout":
            assert saved.status_reason == "Review timed out after 3 seconds"
            assert result.stdout == b"partial analysis\n"
            assert result.stderr == b"partial diagnostic\n"
        else:
            assert saved.status_reason == "Review failed during agent execution (FileNotFoundError)"
            assert str(tmp_path) not in saved.status_reason
    finally:
        await database.close()


async def test_cancelling_target_fetch_does_not_wait_for_network(tmp_path: Path) -> None:
    from tests.test_worker import Execution, prepare

    from diffbutler.core.worker import ReviewWorker

    database = Database(f"sqlite+aiosqlite:///{tmp_path}/cancel-comparison.db")
    await database.initialize()
    task = None
    try:
        forge, identity = await seed(database)
        reviews = service(database)
        run = await reviews.request_review(identity)
        execution = Execution()
        checking = asyncio.Event()

        async def compare(_run: RunRecord, _base: str) -> bool:
            checking.set()
            await asyncio.Event().wait()
            return True

        reviews.compare_base = compare
        task = asyncio.create_task(
            ReviewWorker(
                reviews, prepare, lambda _: execution, poll_interval=0.01
            ).execute_requested(run.id)
        )
        await asyncio.wait_for(execution.started.wait(), 5)
        forge.prs[REF] = replace(forge.prs[REF], base_sha="new-target")
        await DiffButler(SQLiteQueueStore(database), forge).sync()
        await asyncio.wait_for(checking.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert execution.stopped
        assert (await reviews.get_run(run.id)).status_reason == "Review cancelled"
        assert await reviews.get_result(run.id) is not None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await database.close()
