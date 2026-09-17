"""Offline contracts and durable identity; no real reviewer or source access."""

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from diffbutler.core.review import AIRun as RunDTO
from diffbutler.domain.review import AnalysisIdentity, RunStatus, Side, SourceFile
from diffbutler.reviewers.base import (
    ReviewBudget,
    ReviewContext,
    Reviewer,
    ReviewerCapabilities,
    ReviewEvent,
    ReviewOutput,
)
from diffbutler.store.database import Database, migration_config
from diffbutler.store.models import (
    AIRun,
    PullRequest,
    Repository,
    ReviewSession,
    ReviewTask,
    utc_now,
)


def test_analysis_identity_is_stable_and_account_cycle_scoped() -> None:
    identity = AnalysisIdentity(uuid4(), "head", "fake", "v1", "config")
    assert identity.key == replace(identity).key
    for changed in (
        replace(identity, task_id=uuid4()),
        replace(identity, head_sha="new-head"),
        replace(identity, reviewer_id="other"),
        replace(identity, prompt_version="v2"),
        replace(identity, config_hash="changed"),
    ):
        assert identity.key != changed.key
    # Delimiters in config strings cannot alias another analysis identity.
    assert (
        replace(identity, reviewer_id="a|b", prompt_version="c").key
        != replace(identity, reviewer_id="a", prompt_version="b|c").key
    )


@pytest.mark.parametrize(
    "path", ["", "/etc/passwd", "../secret", "a/../b", "a//b", "./a", "a\\b", "a\0b"]
)
def test_source_requests_reject_nonrelative_paths(path: str) -> None:
    with pytest.raises(ValueError, match="source path"):
        SourceFile(path, Side.HEAD)


def test_budget_and_immutable_serializable_run() -> None:
    with pytest.raises(ValueError, match="positive"):
        ReviewBudget(max_output_bytes=0)
    run = RunDTO(
        id=uuid4(),
        task_id=uuid4(),
        session_id=uuid4(),
        reviewer_id="fake",
        head_sha="head",
        base_sha="base",
        prompt_version="v1",
        config_hash="config",
        analysis_key="key",
        attempt=1,
        status=RunStatus.QUEUED,
        requested_at=utc_now(),
        started_at=None,
        finished_at=None,
    )
    assert RunDTO.model_validate_json(run.model_dump_json()) == run
    with pytest.raises(ValidationError, match="frozen"):
        run.status = RunStatus.RUNNING


class FakeReviewer:
    id = "fake"
    capabilities = ReviewerCapabilities()

    async def review(self, context: ReviewContext) -> AsyncIterator[ReviewEvent]:
        yield ReviewOutput(b"synthetic", b"", 0)


def test_async_generator_reviewer_satisfies_protocol() -> None:
    reviewer: Reviewer = FakeReviewer()
    assert reviewer.id == "fake" and not reviewer.capabilities.resume


@pytest.mark.asyncio
async def test_upgrade_persists_queue_then_sessions_and_runs(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/ledger.db"
    database = Database(url)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(lambda conn: command.upgrade(migration_config(conn), "0002"))
        async with database.sessions() as db:
            repo = Repository(forge="example", namespace="acme", name="api", default_branch="main")
            db.add(repo)
            await db.flush()
            pr = PullRequest(
                repository_id=repo.id,
                number=42,
                title="Synthetic",
                author="author",
                head_sha="head",
                base_sha="base",
                head_ref="topic",
                base_ref="main",
                url="https://example.invalid/42",
                state="open",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            db.add(pr)
            await db.flush()
            task = ReviewTask(pull_request_id=pr.id)
            db.add(task)
            await db.commit()
            task_id = task.id
        status = await database.initialize()
        assert status.revision == "0003" and status.backup is not None
        async with database.sessions() as db:
            assert await db.scalar(select(ReviewTask.state)) == "PENDING_AI_REVIEW"
            assert await db.scalar(select(func.count()).select_from(AIRun)) == 0
            conversation = ReviewSession(
                review_task_id=task_id,
                reviewer_id="fake",
                display_name="acme/api#42",
                backend_session_id="opaque-42",
            )
            db.add(conversation)
            await db.commit()
            session_id = conversation.id
            identity = AnalysisIdentity(task_id, "head", "fake", "v1", "config")
            run = AIRun(
                review_task_id=task_id,
                review_session_id=session_id,
                reviewer_id="fake",
                prompt_version="v1",
                config_hash="config",
                head_sha="head",
                base_sha="base",
                analysis_key=identity.key,
                attempt=1,
                stdout_blob=b"synthetic output",
            )
            db.add(run)
            await db.commit()
            run_id = run.id
    finally:
        await database.close()

    reopened = Database(url)
    try:
        await reopened.initialize(auto_migrate=False)
        async with reopened.sessions() as db:
            saved = await db.get(ReviewSession, session_id)
            assert saved is not None and saved.backend_session_id == "opaque-42"
            assert saved.display_name == "acme/api#42" and saved.resource_manifest_json == []
            saved_run = await db.get(AIRun, run_id)
            assert saved_run is not None and saved_run.stdout_blob == b"synthetic output"
            for attempt, reviewer, task_ref in [
                (1, "fake", task_id),
                (2, "other", task_id),
                (2, "fake", uuid4()),
                (0, "fake", task_id),
            ]:
                db.add(
                    AIRun(
                        review_task_id=task_ref,
                        review_session_id=session_id,
                        reviewer_id=reviewer,
                        prompt_version="v1",
                        config_hash="config",
                        head_sha="head",
                        base_sha="base",
                        analysis_key=identity.key,
                        attempt=attempt,
                    )
                )
                with pytest.raises(IntegrityError):
                    await db.commit()
                await db.rollback()
            # A deliberate later attempt retains the same conversation and analysis key.
            db.add(
                AIRun(
                    review_task_id=task_id,
                    review_session_id=session_id,
                    reviewer_id="fake",
                    prompt_version="v1",
                    config_hash="config",
                    head_sha="head",
                    base_sha="base",
                    analysis_key=identity.key,
                    attempt=2,
                )
            )
            await db.commit()
            assert await db.scalar(select(func.count()).select_from(AIRun)) == 2
            db.add(
                ReviewSession(review_task_id=task_id, reviewer_id="fake", display_name="duplicate")
            )
            with pytest.raises(IntegrityError):
                await db.commit()
            await db.rollback()
            # Cleanup may remove payloads without deleting conversation/run audit identity.
            saved = await db.get(ReviewSession, session_id)
            saved_run = await db.get(AIRun, run_id)
            assert saved is not None and saved_run is not None
            saved.status = "cleaned"
            saved.backend_session_id = None
            saved_run.stdout_blob = None
            db.add(
                ReviewSession(
                    review_task_id=task_id,
                    reviewer_id="fake",
                    generation=2,
                    display_name="acme/api#42",
                )
            )
            await db.commit()
            assert await db.scalar(select(func.count()).select_from(ReviewSession)) == 2
    finally:
        await reopened.close()
