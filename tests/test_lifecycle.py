"""Offline ownership, completion races, durable cleanup and retained-history tests."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from tests.test_review_claims import seed, service
from tests.test_review_results import captured
from tests.test_sync import ACCOUNT, PR, REF

from diffbutler.core.lifecycle import LifecycleService
from diffbutler.core.sync import DiffButler
from diffbutler.domain.forge import Discovery
from diffbutler.domain.lifecycle import CleanupSession
from diffbutler.domain.review import ReviewConflict
from diffbutler.store.base import SyncConflict
from diffbutler.store.dashboard import SQLiteDashboardStore
from diffbutler.store.database import Database
from diffbutler.store.lifecycle import SQLiteLifecycleStore
from diffbutler.store.models import (
    CleanupClaim,
    CleanupJob,
    ForegroundLease,
    ReviewSession,
    ReviewTask,
    utc_now,
)
from diffbutler.store.queue import SQLiteQueueStore

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncGenerator[Database]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/lifecycle.db")
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


class Cleaner:
    def __init__(self) -> None:
        self.calls: list[CleanupSession] = []
        self.fail = False
        self.cancel = False

    async def cleanup(self, session: CleanupSession) -> None:
        self.calls.append(session)
        if self.cancel:
            raise asyncio.CancelledError
        if self.fail:
            raise OSError("private backend error")


async def test_foreground_survives_restart_blocks_background_and_defers_cleanup(
    database: Database,
) -> None:
    forge, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    await reviews.finalize_review(claim.token, captured())
    cleaner = Cleaner()
    lifecycle = LifecycleService(SQLiteLifecycleStore(database), cleaner)
    lease = await lifecycle.acquire_foreground(run.session_id)
    assert lease.backend_session_id == "fake-id" and lease.head_sha == run.head_sha
    reopened = Database(str(database.url))
    try:
        restored = LifecycleService(SQLiteLifecycleStore(reopened), cleaner)
        with pytest.raises(ReviewConflict, match="Foreground"):
            await restored.acquire_foreground(run.session_id)
        with pytest.raises(ReviewConflict, match="Foreground"):
            await service(reopened).request_review(identity)
        with pytest.raises(ReviewConflict, match="Foreground"):
            await service(reopened).suspend_session(run.session_id)
        forge.prs[REF] = replace(PR, state="closed")
        await DiffButler(SQLiteQueueStore(reopened), forge).sync()
        assert not await restored.cleanup_once()
        assert not cleaner.calls
        assert (await SQLiteQueueStore(reopened).snapshot(ACCOUNT)).items[0].state == "DONE"
        await restored.release_foreground(lease.token)
        assert await restored.cleanup_once()
        assert not await restored.cleanup_once()
        session = await reviews.get_session(run.session_id)
        assert session.status == "cleaned" and session.backend_session_id is None
        result = await reviews.get_result(run.id)
        assert result is not None and result.document is not None
        assert result.stdout is None and result.stderr is None
        assert cleaner.calls[0].backend_session_id == "fake-id"
        async with reopened.sessions() as db:
            assert await db.get(ForegroundLease, lease.token) is None
            job = await db.scalar(select(CleanupJob))
            assert job is not None and job.status == "succeeded" and job.attempts == 1
        with pytest.raises(ReviewConflict):
            await restored.release_foreground(lease.token)
    finally:
        await reopened.close()


async def test_completion_preserves_worker_claim_until_exit_before_cleanup(
    database: Database,
) -> None:
    forge, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    await reviews.bind_backend_session(claim.token, "backend")
    lifecycle = LifecycleService(SQLiteLifecycleStore(database), Cleaner())
    with pytest.raises(ReviewConflict, match="Background"):
        await lifecycle.acquire_foreground(run.session_id)
    forge.prs[REF] = replace(PR, state="closed")
    await DiffButler(SQLiteQueueStore(database), forge).sync()
    assert not await lifecycle.cleanup_once()
    await reviews.release_review(claim.token)
    assert await lifecycle.cleanup_once()


async def test_failed_cleanup_retries_idempotently_without_resurrecting_task(
    database: Database,
) -> None:
    forge, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    await reviews.finalize_review(claim.token, captured())
    async with database.sessions() as db:
        session = await db.get(ReviewSession, run.session_id)
        assert session is not None
        session.resource_manifest_json = [{"kind": "artifact", "id": "owned-resource"}]
        session.transcript_ref = "owned-transcript"
        await db.commit()
    forge.prs[REF] = replace(PR, state="closed")
    await DiffButler(SQLiteQueueStore(database), forge).sync()
    cleaner = Cleaner()
    cleaner.fail = True
    lifecycle = LifecycleService(SQLiteLifecycleStore(database), cleaner)
    assert await lifecycle.cleanup_once()
    assert not await lifecycle.cleanup_once()  # Backoff is durable.
    async with database.sessions() as db:
        job = await db.scalar(select(CleanupJob))
        assert job is not None and job.status == "failed" and job.last_error is not None
        assert "private" not in job.last_error
        job.next_retry_at = utc_now() - timedelta(seconds=1)
        session = await db.get(ReviewSession, run.session_id)
        assert session is not None and session.resource_manifest_json
        assert session.backend_session_id == "fake-id"
        await db.commit()
    cleaner.fail = False
    assert await lifecycle.cleanup_once()
    assert cleaner.calls[0] == cleaner.calls[1]
    assert (await SQLiteQueueStore(database).snapshot(ACCOUNT)).items[0].state == "DONE"
    result = await reviews.get_result(run.id)
    assert result is not None and result.document is not None and result.stdout is None


async def test_cleanup_cancellation_retains_claim_until_supervisor_acknowledges_exit(
    database: Database,
) -> None:
    forge, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    await reviews.finalize_review(claim.token, captured())
    forge.prs[REF] = replace(PR, state="closed")
    await DiffButler(SQLiteQueueStore(database), forge).sync()
    cleaner = Cleaner()
    cleaner.cancel = True
    store = SQLiteLifecycleStore(database)
    with pytest.raises(asyncio.CancelledError):
        await LifecycleService(store, cleaner).cleanup_once()
    assert await store.claim_cleanup() is None
    async with database.sessions() as db:
        held = await db.scalar(select(CleanupClaim))
        assert held is not None
        token = held.token
    # Trusted supervisor establishes that the fake operation has ended.
    await store.finish_cleanup(token, succeeded=False)
    assert (await reviews.get_session(run.session_id)).status != "cleaned"


async def test_acquire_race_and_push_during_foreground_preserve_conversation(
    database: Database,
) -> None:
    forge, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    await reviews.finalize_review(claim.token, captured())
    store = SQLiteLifecycleStore(database)
    attempts = await asyncio.gather(
        store.acquire_foreground(run.session_id),
        store.acquire_foreground(run.session_id),
        return_exceptions=True,
    )
    leases = [item for item in attempts if not isinstance(item, BaseException)]
    assert len(leases) == 1
    assert sum(isinstance(item, ReviewConflict) for item in attempts) == 1
    forge.prs[REF] = replace(PR, head_sha="new-head")
    await DiffButler(SQLiteQueueStore(database), forge).sync()
    assert (await reviews.get_session(run.session_id)).backend_session_id == "fake-id"
    with pytest.raises(ReviewConflict):
        await reviews.request_review(replace(identity, head_sha="new-head"))
    await store.release_foreground(leases[0].token)
    retry = await reviews.request_review(replace(identity, head_sha="new-head"))
    assert retry.session_id == run.session_id and retry.head_sha == "new-head"


async def test_cleanup_without_sessions_and_claim_race(database: Database) -> None:
    forge, _ = await seed(database)
    forge.prs[REF] = replace(PR, state="closed")
    await DiffButler(SQLiteQueueStore(database), forge).sync()
    store = SQLiteLifecycleStore(database)
    claims = await asyncio.gather(store.claim_cleanup(), store.claim_cleanup())
    claim = next(item for item in claims if item is not None)
    assert sum(item is not None for item in claims) == 1 and claim.sessions == ()
    await store.finish_cleanup(claim.token, succeeded=True)
    with pytest.raises(ReviewConflict):
        await store.finish_cleanup(claim.token, succeeded=True)


async def test_ownership_recovery_listing_survives_restart(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    await reviews.finalize_review(claim.token, captured())
    store = SQLiteLifecycleStore(database)
    lease = await store.acquire_foreground(run.session_id)
    reopened = Database(str(database.url))
    try:
        restored = LifecycleService(SQLiteLifecycleStore(reopened), Cleaner())
        operations = await restored.held_operations()
        assert len(operations) == 1
        assert operations[0].token == lease.token and operations[0].kind == "foreground"
        assert operations[0].resource_id == run.session_id
        await restored.release_foreground(lease.token)
        assert not await restored.held_operations()
    finally:
        await reopened.close()


@pytest.mark.parametrize("state", ["idle", "queued", "running", "finished", "foreground"])
async def test_pr_reset_retains_history_and_ownership_until_exit(
    database: Database, state: str
) -> None:
    forge, identity = await seed(database)
    reviews = service(database)
    dashboard = SQLiteDashboardStore(database)
    queue = SQLiteQueueStore(database)
    lifecycle = LifecycleService(SQLiteLifecycleStore(database), Cleaner())
    run = await reviews.request_review(identity) if state != "idle" else None
    claim = await reviews.claim_review(run.id) if run and state != "queued" else None
    if claim and state in {"finished", "foreground"}:
        await reviews.finalize_review(claim.token, captured())
    lease = (
        await lifecycle.acquire_foreground(run.session_id)
        if run and state == "foreground"
        else None
    )
    before = await queue.snapshot(ACCOUNT)
    # Simulate a pre-snapshot migration cycle before removing its monitor.
    async with database.sessions() as db:
        task = await db.get(ReviewTask, identity.task_id)
        assert task is not None
        task.snapshot_json = None
        await db.commit()
    await dashboard.reset_task(identity.task_id)
    assert not (await queue.snapshot(ACCOUNT)).items
    with pytest.raises(SyncConflict):
        await queue.apply(ACCOUNT, before.version, before.items)
    with pytest.raises(ReviewConflict):
        await reviews.request_review(identity)
    with pytest.raises(ReviewConflict):
        await dashboard.reset_task(identity.task_id)
    cycles = await dashboard.cycles()
    assert len(cycles) == 1
    assert cycles[0].completion_reason == "local_reset"
    assert cycles[0].pull_request == PR
    assert cycles[0].cleanup_status == "pending"
    if run:
        assert (await reviews.get_run(run.id)).status == (
            "succeeded" if state in {"finished", "foreground"} else "cancelled"
        )
    if state == "running":
        assert claim is not None
        assert not await lifecycle.cleanup_once()
        assert not await reviews.claim_is_current(claim.token)
        await reviews.release_review(claim.token)
    if lease:
        assert not await lifecycle.cleanup_once()
        await lifecycle.release_foreground(lease.token)
    assert await lifecycle.cleanup_once()
    assert (await dashboard.cycles())[0].cleanup_status == "succeeded"
    if run:
        assert (await reviews.get_session(run.session_id)).status == "cleaned"
    if state in {"finished", "foreground"}:
        assert run is not None
        result = await reviews.get_result(run.id)
        assert result is not None and result.document is not None and result.stdout is None

    # Following is forgotten; absent requests cannot bring the PR back.
    discovery = forge.discovery
    forge.discovery = Discovery((), True)
    await DiffButler(queue, forge).sync()
    assert not (await queue.snapshot(ACCOUNT)).items
    assert not forge.lookups
    forge.discovery = discovery
    for cycle_number in (2, 3):
        await DiffButler(queue, forge).sync()
        fresh = (await queue.snapshot(ACCOUNT)).items[0]
        assert fresh.task_id is not None and fresh.task_id != identity.task_id
        assert fresh.cycle == cycle_number and fresh.state == "PENDING_AI_REVIEW"
        assert await reviews.store.latest_run(fresh.task_id) is None
        await dashboard.reset_task(fresh.task_id)
    assert len(await dashboard.cycles()) == 3
