"""Explicit scheduling acceptance; no processes, model requests, or forge writes."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import func, select
from tests.test_sync import ACCOUNT, NOW, PR, REF, FakeForge

from diffbutler.core.review import ReviewService
from diffbutler.core.sync import DiffButler
from diffbutler.domain.review import AnalysisIdentity, ReviewConflict, ReviewPolicy, RunStatus
from diffbutler.domain.sync import RequestEvent, ReviewActivity
from diffbutler.store.base import SyncConflict
from diffbutler.store.database import Database, migration_config
from diffbutler.store.models import AIRun, CleanupJob, ReviewSession, RunClaim
from diffbutler.store.queue import SQLiteQueueStore
from diffbutler.store.review import SQLiteReviewStore

POLICY = ReviewPolicy(frozenset({"fake", "other"}))


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncGenerator[Database]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/claims.db")
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


async def seed(db: Database) -> tuple[FakeForge, AnalysisIdentity]:
    forge = FakeForge()
    queue = SQLiteQueueStore(db)
    await DiffButler(queue, forge).sync()
    task_id = (await queue.snapshot(ACCOUNT)).items[0].task_id
    assert task_id is not None
    return forge, AnalysisIdentity(task_id, "head", "fake", "v1", "config")


def service(db: Database) -> ReviewService:
    return ReviewService(SQLiteReviewStore(db), POLICY)


@pytest.mark.asyncio
async def test_concurrent_start_claim_and_deliberate_retry(database: Database) -> None:
    _, identity = await seed(database)
    first, second = service(database), service(database)
    runs = await asyncio.gather(*(s.request_review(identity) for s in (first, second)))
    assert runs[0].id == runs[1].id
    assert runs[0].status == RunStatus.QUEUED
    with pytest.raises(ReviewConflict, match="Another analysis"):
        await first.request_review(replace(identity, reviewer_id="other"))
    claims = await asyncio.gather(*(s.claim_review(runs[0].id) for s in (first, second)))
    claim = next(c for c in claims if c is not None)
    assert sum(c is not None for c in claims) == 1
    assert claim.run.status == RunStatus.RUNNING
    assert (await first.request_review(identity)).id == runs[0].id
    failed = await first.release_review(claim.token)
    assert failed.status == RunStatus.FAILED
    queue = await SQLiteQueueStore(database).snapshot(ACCOUNT)
    assert queue.items[0].state == "PENDING_HUMAN_REVIEW"
    retry = await first.request_review(identity)
    assert retry.attempt == 2 and retry.session_id == runs[0].session_id
    assert retry.analysis_key == runs[0].analysis_key
    with pytest.raises(ReviewConflict, match="claim"):
        await first.release_review(claim.token)


@pytest.mark.asyncio
async def test_sync_cannot_overwrite_a_start(database: Database) -> None:
    forge, identity = await seed(database)
    queue = SQLiteQueueStore(database)
    old = await queue.snapshot(ACCOUNT)
    run = await service(database).request_review(identity)
    with pytest.raises(SyncConflict):
        await queue.apply(ACCOUNT, old.version, old.items)
    await DiffButler(queue, forge).sync()
    assert (await queue.snapshot(ACCOUNT)).items[0].state == "QUEUED"
    assert (await service(database).request_review(identity)).id == run.id


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("change", ["push", "request", "closed"])
async def test_sync_invalidates_without_replacement(
    database: Database, claimed: bool, change: str
) -> None:
    forge, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id) if claimed else None
    if change == "push":
        forge.prs[REF] = replace(PR, head_sha="new")
    elif change == "request":
        forge.activity = ReviewActivity(
            ACCOUNT, requests=(RequestEvent("renewed", NOW + timedelta(seconds=1)),)
        )
    else:
        forge.prs[REF] = replace(PR, state="closed")
    await DiffButler(SQLiteQueueStore(database), forge).sync()
    async with database.sessions() as db:
        saved = await db.get(AIRun, run.id)
        assert saved is not None
        assert saved.status == ("cancelled" if change == "closed" else "stale")
        assert await db.scalar(select(func.count()).select_from(AIRun)) == 1
        assert await db.scalar(select(func.count()).select_from(RunClaim)) == int(claimed)
        assert await db.scalar(select(func.count()).select_from(CleanupJob)) == int(
            change == "closed"
        )
    assert await reviews.claim_review(run.id) is None
    if claim is not None:
        if change != "closed":
            with pytest.raises(ReviewConflict, match="ownership"):
                await reviews.request_review(
                    replace(identity, head_sha="new" if change == "push" else "head")
                )
        await reviews.release_review(claim.token)
    state = (await SQLiteQueueStore(database).snapshot(ACCOUNT)).items[0].state
    assert state == ("DONE" if change == "closed" else "PENDING_AI_REVIEW")
    if change == "closed":
        with pytest.raises(ReviewConflict, match="completed"):
            await reviews.request_review(identity)


@pytest.mark.asyncio
async def test_cancel_restart_and_fresh_session(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    assert (await reviews.cancel_review(run.id)).status == RunStatus.CANCELLED
    assert (await reviews.cancel_review(run.id)).status == RunStatus.CANCELLED
    reopened = Database(str(database.url))
    try:
        await reopened.initialize(auto_migrate=False)
        restored = service(reopened)
        with pytest.raises(ReviewConflict, match="ownership"):
            await restored.request_review(identity)
        with pytest.raises(ReviewConflict, match="claim"):
            await restored.release_review(uuid4())
        await restored.release_review(claim.token)
        async with reopened.sessions() as db:
            conversation = await db.get(ReviewSession, run.session_id)
            assert conversation is not None
            conversation.status = "cleaned"
            await db.commit()
        retry = await restored.request_review(identity)
        assert retry.session_id != run.session_id and retry.attempt == 2
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_policy_pause_and_capacity(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    disabled = ReviewService(
        SQLiteReviewStore(database), ReviewPolicy(POLICY.reviewer_ids, frozenset({"acme/api"}))
    )
    with pytest.raises(ReviewConflict, match="policy"):
        await disabled.request_review(identity)
    with pytest.raises(ReviewConflict, match="policy"):
        await reviews.request_review(replace(identity, reviewer_id="unknown"))
    with pytest.raises(ReviewConflict, match="head changed"):
        await reviews.request_review(replace(identity, head_sha="old"))
    run = await reviews.request_review(identity)
    assert await disabled.claim_review(run.id) is None
    assert await reviews.claim_review(run.id, paused=True) is None
    with pytest.raises(ValueError, match="positive"):
        await reviews.claim_review(run.id, capacity=0)
    claim = await reviews.claim_review(run.id, capacity=1)
    assert claim is not None
    # A second account's task is distinct, but shares the database worker pool.
    forge = FakeForge()
    forge.account = replace(ACCOUNT, login="second")
    forge.activity = ReviewActivity(forge.account)
    queue = SQLiteQueueStore(database)
    await DiffButler(queue, forge).follow_pr(REF)
    task_id = (await queue.snapshot(forge.account)).items[0].task_id
    assert task_id is not None
    other = await reviews.request_review(replace(identity, task_id=task_id))
    assert other.session_id != run.session_id
    assert await reviews.claim_review(other.id, capacity=1) is None
    await reviews.cancel_review(run.id)
    assert await reviews.claim_review(other.id, capacity=1) is None
    await reviews.release_review(claim.token)
    assert await reviews.claim_review(other.id, capacity=1) is not None


@pytest.mark.asyncio
async def test_cancel_queued_and_passive_actions_create_no_runs(database: Database) -> None:
    forge, identity = await seed(database)
    queue = SQLiteQueueStore(database)
    sync = DiffButler(queue, forge)
    await sync.follow_pr(REF)
    await sync.sync()
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(AIRun)) == 0
        assert await db.scalar(select(func.count()).select_from(ReviewSession)) == 0
    reviews = service(database)
    run = await reviews.request_review(identity)
    await reviews.cancel_review(run.id)
    assert await reviews.claim_review(run.id) is None
    retry = await reviews.request_review(identity)
    assert retry.attempt == 2 and retry.session_id == run.session_id


@pytest.mark.asyncio
async def test_upgrade_0003_preserves_queued_attempt(database: Database) -> None:
    _, identity = await seed(database)
    run = await service(database).request_review(identity)
    async with database.engine.begin() as connection:
        await connection.run_sync(lambda conn: command.downgrade(migration_config(conn), "0003"))
    status = await database.initialize()
    assert status.revision == "0006" and status.backup is not None
    assert (await service(database).request_review(identity)).id == run.id
    assert await service(database).claim_review(run.id) is not None
