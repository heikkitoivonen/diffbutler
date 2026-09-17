"""Worker ownership tests use trusted in-memory executions, never real reviewers."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from tests.test_context import FakeSources
from tests.test_review_claims import seed, service
from tests.test_review_results import captured
from tests.test_sync import PR, REF

from diffbutler.core.context import assemble_context
from diffbutler.core.sync import DiffButler
from diffbutler.core.worker import ReviewWorker
from diffbutler.domain.review import RunRecord, RunStatus
from diffbutler.reviewers.base import ReviewBudget, ReviewContext, ReviewOutput
from diffbutler.store.database import Database
from diffbutler.store.models import RunClaim
from diffbutler.store.queue import SQLiteQueueStore

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncGenerator[Database]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/worker.db")
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


async def prepare(run: RunRecord) -> ReviewContext:
    return await assemble_context(
        run=run,
        pr=PR,
        sources=FakeSources(),
        instructions="source only",
        schema_json="{}",
        budget=ReviewBudget(timeout_s=1),
    )


class Execution:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.stop_started = asyncio.Event()
        self.exit_allowed = asyncio.Event()
        self.exit_allowed.set()
        self.stop_failed = False
        self.stopped = False
        self.resumed: str | None = None

    async def run(
        self,
        context: ReviewContext,
        backend_session_id: str | None,
        checkpoint: Callable[[str], Awaitable[None]],
    ) -> ReviewOutput:
        self.resumed = backend_session_id
        await checkpoint(backend_session_id or "fake-id")
        self.started.set()
        await self.finished.wait()
        return captured()

    async def stop_and_wait(self) -> None:
        self.stop_started.set()
        await self.exit_allowed.wait()
        if self.stop_failed:
            raise RuntimeError("cannot establish exit")
        self.stopped = True
        self.finished.set()


async def test_success_waits_for_exit_then_restart_resumes_exact_identity(
    database: Database,
) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    execution = Execution()
    execution.finished.set()
    execution.exit_allowed.clear()
    task = asyncio.create_task(
        ReviewWorker(reviews, prepare, lambda: execution).execute_requested(run.id)
    )
    await execution.stop_started.wait()
    assert await reviews.get_result(run.id) is None
    async with database.sessions() as db:
        assert await db.scalar(select(RunClaim)) is not None
    execution.exit_allowed.set()
    result = await task
    assert result is not None and result.status == RunStatus.SUCCEEDED
    assert execution.stopped
    reopened = Database(str(database.url))
    try:
        retry = await service(reopened).request_review(identity)
        resumed = Execution()
        resumed.finished.set()
        await ReviewWorker(service(reopened), prepare, lambda: resumed).execute_requested(retry.id)
        assert resumed.resumed == "fake-id"
    finally:
        await reopened.close()


@pytest.mark.parametrize("change", ["cancel", "push", "closed", "timeout"])
async def test_stop_before_release_without_replacement_run(database: Database, change: str) -> None:
    forge, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    execution = Execution()
    task = asyncio.create_task(
        ReviewWorker(reviews, prepare, lambda: execution, poll_interval=0.01).execute_requested(
            run.id
        )
    )
    await execution.started.wait()
    if change == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif change == "timeout":
        with pytest.raises(TimeoutError):
            await task
    else:
        forge.prs[REF] = (
            replace(PR, head_sha="new") if change == "push" else replace(PR, state="closed")
        )
        await DiffButler(SQLiteQueueStore(database), forge).sync()
        result = await task
        assert result is not None
        assert result.status == (RunStatus.STALE if change == "push" else RunStatus.CANCELLED)
    assert execution.stopped
    async with database.sessions() as db:
        assert await db.scalar(select(RunClaim)) is None
    assert await reviews.get_result(run.id) is None


async def test_failed_exit_keeps_durable_ownership(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    execution = Execution()
    execution.finished.set()
    execution.stop_failed = True
    with pytest.raises(RuntimeError, match="establish exit"):
        await ReviewWorker(reviews, prepare, lambda: execution).execute_requested(run.id)
    async with database.sessions() as db:
        assert await db.scalar(select(RunClaim)) is not None
    assert await reviews.get_result(run.id) is None


async def test_paused_worker_and_stale_preparation_never_launch(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    execution = Execution()
    worker = ReviewWorker(reviews, prepare, lambda: execution)
    assert await worker.execute_requested(run.id, paused=True) is None
    assert not execution.started.is_set()

    async def invalidated(run: RunRecord) -> ReviewContext:
        context = await prepare(run)
        await reviews.cancel_review(run.id)
        return context

    result = await ReviewWorker(reviews, invalidated, lambda: execution).execute_requested(run.id)
    assert result is not None and result.status == RunStatus.CANCELLED
    assert not execution.started.is_set()


async def test_base_move_during_preparation_is_stale_without_launch(database: Database) -> None:
    forge, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    execution = Execution()

    async def moved(record: RunRecord) -> ReviewContext:
        context = await prepare(record)
        forge.prs[REF] = replace(PR, base_sha="new-base")
        await DiffButler(SQLiteQueueStore(database), forge).sync()
        return context

    result = await ReviewWorker(reviews, moved, lambda: execution).execute_requested(run.id)
    assert result is not None and result.status == RunStatus.STALE
    assert not execution.started.is_set()


async def test_repeated_cancellation_during_stop_retains_claim_until_exit(
    database: Database,
) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    execution = Execution()
    execution.exit_allowed.clear()
    task = asyncio.create_task(
        ReviewWorker(reviews, prepare, lambda: execution).execute_requested(run.id)
    )
    await execution.started.wait()
    task.cancel()
    await execution.stop_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    async with database.sessions() as db:
        assert await db.scalar(select(RunClaim)) is not None
    execution.exit_allowed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert execution.stopped
    async with database.sessions() as db:
        assert await db.scalar(select(RunClaim)) is None
