"""Durable conversation checkpoints, using only a synthetic executable and forge."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from tests.test_review_claims import database as database
from tests.test_review_claims import seed, service
from tests.test_sync import ACCOUNT, PR, REF, FakeForge

from diffbutler.core.review import ReviewSession as SessionDTO
from diffbutler.core.sync import DiffButler
from diffbutler.domain.review import ReviewConflict, RunStatus, SessionStatus
from diffbutler.domain.sync import ReviewActivity
from diffbutler.store.database import Database
from diffbutler.store.models import AIRun, ReviewSession, RunClaim
from diffbutler.store.queue import SQLiteQueueStore


async def fake_turn(workspace: Path, backend_id: str | None) -> tuple[str, int]:
    # This launcher exists only in the test harness, accepts only our fixture,
    # and sends no repository sources or credentials to any real executable.
    args = ("start",) if backend_id is None else ("resume", backend_id)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(Path(__file__).parent / "fixtures" / "fake_session_cli.py"),
        *args,
        cwd=workspace,
        env={},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(5):
            stdout, stderr = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
    assert process.returncode == 0, stderr
    result = json.loads(stdout)
    assert isinstance(result["session_id"], str) and isinstance(result["turn"], int)
    return result["session_id"], result["turn"]


@pytest.mark.asyncio
async def test_fake_cli_conversation_survives_restart_and_explicit_retry(
    database: Database, tmp_path: Path
) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    before = await reviews.get_session(run.session_id)
    assert before.backend_session_id is None and before.display_name == "acme/api#1"
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    backend_id, turn = await fake_turn(tmp_path, before.backend_session_id)
    assert turn == 1
    saved = await reviews.bind_backend_session(claim.token, backend_id)
    assert saved.id == before.id and saved.backend_session_id == backend_id
    assert SessionDTO.model_validate_json(saved.model_dump_json()) == saved
    with pytest.raises(ValidationError, match="frozen"):
        saved.backend_session_id = "changed"
    # This increment does not parse successful review results: release still
    # records failure, while preserving the independently checkpointed session.
    assert (await reviews.release_review(claim.token)).status == RunStatus.FAILED
    await reviews.suspend_session(saved.id)
    await database.close()

    reopened = Database(str(database.url))
    try:
        await reopened.initialize(auto_migrate=False)
        restored = service(reopened)
        conversation = await restored.get_session(saved.id)
        assert conversation.backend_session_id == backend_id
        assert conversation.status == SessionStatus.SUSPENDED
        async with reopened.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(AIRun)) == 1
        retry = await restored.request_review(identity)
        assert retry.session_id == saved.id and retry.attempt == 2
        second = await restored.claim_review(retry.id)
        assert second is not None
        conversation = await restored.get_session(retry.session_id)
        assert conversation.status == SessionStatus.READY
        resumed_id, turn = await fake_turn(tmp_path, conversation.backend_session_id)
        assert resumed_id == backend_id and turn == 2
        await restored.bind_backend_session(second.token, resumed_id)
        await restored.release_review(second.token)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_binding_requires_claim_and_never_replaces_an_identity(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    with pytest.raises(ReviewConflict, match="claim"):
        await reviews.bind_backend_session(uuid4(), "unowned")
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    results = await asyncio.gather(
        reviews.bind_backend_session(claim.token, "first"),
        reviews.bind_backend_session(claim.token, "second"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ReviewConflict) for result in results) == 1
    saved = await reviews.get_session(run.session_id)
    assert saved.backend_session_id in {"first", "second"}
    assert await reviews.bind_backend_session(claim.token, saved.backend_session_id) == saved
    await reviews.release_review(claim.token)
    with pytest.raises(ReviewConflict, match="claim"):
        await reviews.bind_backend_session(claim.token, saved.backend_session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "  ", "x" * 1025, "bad\0id", "bad\nid", "bad\x85id"])
async def test_backend_identifier_validation(database: Database, value: str) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    with pytest.raises(ValueError, match="Backend session ID"):
        await reviews.bind_backend_session(claim.token, value)
    assert (await reviews.get_session(run.session_id)).backend_session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancel", "push", "closed"])
async def test_late_checkpoint_preserves_invalidated_state_and_ownership(
    database: Database, change: str
) -> None:
    forge, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    if change == "cancel":
        await reviews.cancel_review(run.id)
    else:
        forge.prs[REF] = (
            replace(PR, head_sha="new") if change == "push" else replace(PR, state="closed")
        )
        await DiffButler(SQLiteQueueStore(database), forge).sync()
    before = await reviews.get_run(run.id)
    snapshot = await SQLiteQueueStore(database).snapshot(ACCOUNT)
    opaque = "opaque $(touch never); --resume=other"
    assert (await reviews.bind_backend_session(claim.token, opaque)).backend_session_id == opaque
    assert await reviews.get_run(run.id) == before
    assert await SQLiteQueueStore(database).snapshot(ACCOUNT) == snapshot
    async with database.sessions() as db:
        assert await db.get(RunClaim, claim.token) is not None
    if change == "closed":
        with pytest.raises(ReviewConflict, match="completed"):
            await reviews.suspend_session(run.session_id)
    await reviews.release_review(claim.token)
    assert (await reviews.get_session(run.session_id)).backend_session_id == opaque


@pytest.mark.asyncio
async def test_suspend_requires_idle_session_and_preserves_queue(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    with pytest.raises(ReviewConflict, match="queued work"):
        await reviews.suspend_session(run.session_id)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    await reviews.bind_backend_session(claim.token, "saved")
    await reviews.cancel_review(run.id)
    with pytest.raises(ReviewConflict, match="ownership"):
        await reviews.suspend_session(run.session_id)
    await reviews.release_review(claim.token)
    snapshot = await SQLiteQueueStore(database).snapshot(ACCOUNT)
    first = await reviews.suspend_session(run.session_id)
    assert first.status == SessionStatus.SUSPENDED and first.backend_session_id == "saved"
    assert await reviews.suspend_session(run.session_id) == first
    assert await SQLiteQueueStore(database).snapshot(ACCOUNT) == snapshot
    async with database.sessions() as db:
        saved = await db.get(ReviewSession, run.session_id)
        assert saved is not None
        saved.cleanup_status = "pending"
        await db.commit()
    with pytest.raises(ReviewConflict, match="cleanup"):
        await reviews.suspend_session(run.session_id)


@pytest.mark.asyncio
async def test_cleaned_session_cannot_be_bound_or_claimed_and_retry_has_no_old_id(
    database: Database,
) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    run = await reviews.request_review(identity)
    claim = await reviews.claim_review(run.id)
    assert claim is not None
    await reviews.bind_backend_session(claim.token, "old-id")
    # Simulate future cleanup state defensively; actual cleanup must wait for exit.
    async with database.sessions() as db:
        saved = await db.get(ReviewSession, run.session_id)
        assert saved is not None
        saved.status = "cleaned"
        await db.commit()
    with pytest.raises(ReviewConflict, match="cleaned"):
        await reviews.bind_backend_session(claim.token, "old-id")
    with pytest.raises(ReviewConflict, match="cleaned"):
        await reviews.suspend_session(run.session_id)
    await reviews.release_review(claim.token)
    retry = await reviews.request_review(identity)
    assert retry.session_id != run.session_id
    fresh = await reviews.get_session(retry.session_id)
    assert fresh.generation == 2 and fresh.backend_session_id is None
    async with database.sessions() as db:
        saved = await db.get(ReviewSession, retry.session_id)
        assert saved is not None
        saved.cleanup_status = "pending"
        await db.commit()
    with pytest.raises(ReviewConflict, match="cleanup"):
        await reviews.claim_review(retry.id)
    assert (await reviews.get_run(retry.id)).status == RunStatus.QUEUED


@pytest.mark.asyncio
async def test_unknown_sessions_report_conflict(database: Database) -> None:
    with pytest.raises(ReviewConflict, match="does not exist"):
        await service(database).get_session(uuid4())
    with pytest.raises(ReviewConflict, match="does not exist"):
        await service(database).suspend_session(uuid4())


@pytest.mark.asyncio
async def test_claims_bind_only_their_own_account_and_reviewer_sessions(database: Database) -> None:
    _, identity = await seed(database)
    reviews = service(database)
    first = await reviews.request_review(identity)
    first_claim = await reviews.claim_review(first.id)
    assert first_claim is not None
    forge = FakeForge()
    forge.account = replace(ACCOUNT, login="second")
    forge.activity = ReviewActivity(forge.account)
    queue = SQLiteQueueStore(database)
    await DiffButler(queue, forge).follow_pr(REF)
    other_task = (await queue.snapshot(forge.account)).items[0].task_id
    assert other_task is not None
    second = await reviews.request_review(
        replace(identity, task_id=other_task, reviewer_id="other")
    )
    second_claim = await reviews.claim_review(second.id)
    assert second_claim is not None
    await reviews.bind_backend_session(first_claim.token, "first-account")
    await reviews.bind_backend_session(second_claim.token, "second-account")
    first_session = await reviews.get_session(first.session_id)
    second_session = await reviews.get_session(second.session_id)
    assert first_session.display_name == second_session.display_name
    assert first_session.backend_session_id == "first-account"
    assert second_session.backend_session_id == "second-account"
    assert first_session.task_id != second_session.task_id
    assert first_session.reviewer_id != second_session.reviewer_id
