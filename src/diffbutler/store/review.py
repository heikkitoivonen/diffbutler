"""Short SQLite transactions serialize explicit starts, sync, and worker claims."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from diffbutler.domain.review import (
    AnalysisIdentity,
    ReviewConflict,
    ReviewPolicy,
    RunRecord,
    RunStatus,
)
from diffbutler.domain.review import (
    RunClaim as Claim,
)
from diffbutler.store.database import Database
from diffbutler.store.models import (
    AIRun,
    ForgeAccount,
    PRMonitor,
    PullRequest,
    Repository,
    ReviewSession,
    ReviewTask,
    RunClaim,
    utc_now,
)


def record(run: AIRun) -> RunRecord:
    return RunRecord(
        run.id,
        run.review_task_id,
        run.review_session_id,
        run.reviewer_id,
        run.head_sha,
        run.base_sha,
        run.prompt_version,
        run.config_hash,
        run.analysis_key,
        run.attempt,
        RunStatus(run.status),
        run.requested_at,
        run.started_at,
        run.finished_at,
    )


async def transition(db: AsyncSession, task: ReviewTask, state: str) -> None:
    task.state = state
    task.action_version += 1
    task.updated_at = utc_now()
    task.state_source = "local"
    identity = await db.get(ForgeAccount, task.account_id)
    if identity is not None:
        # A network refresh read before this local action must retry its snapshot.
        identity.version += 1


class SQLiteReviewStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def _begin(self, db: AsyncSession) -> None:
        connection = await db.connection()
        await connection.exec_driver_sql("BEGIN IMMEDIATE")

    async def _task(self, db: AsyncSession, task_id: UUID) -> tuple[ReviewTask, PRMonitor]:
        task = await db.get(ReviewTask, task_id)
        if task is None or task.completed_at is not None:
            raise ReviewConflict("Review task is missing or completed")
        monitor = await db.get(PRMonitor, (task.account_id, task.pull_request_id))
        if (
            monitor is None
            or monitor.task_id != task.id
            or monitor.snapshot_json["state"] != "open"
        ):
            raise ReviewConflict("Review task is no longer current and open")
        return task, monitor

    async def _run(self, db: AsyncSession, run_id: UUID) -> AIRun:
        run = await db.get(AIRun, run_id)
        if run is None:
            raise ReviewConflict("Review run does not exist")
        return run

    async def get_run(self, run_id: UUID) -> RunRecord:
        async with self.database.sessions() as db:
            return record(await self._run(db, run_id))

    async def request_review(self, identity: AnalysisIdentity, policy: ReviewPolicy) -> RunRecord:
        async with self.database.sessions() as db:
            await self._begin(db)
            task, monitor = await self._task(db, identity.task_id)
            if monitor.snapshot_json["head_sha"] != identity.head_sha:
                raise ReviewConflict("PR head changed; refresh before starting")
            pr = await db.get(PullRequest, task.pull_request_id)
            assert pr is not None
            repo = await db.get(Repository, pr.repository_id)
            assert repo is not None
            if (
                not repo.enabled
                or f"{repo.namespace}/{repo.name}" in policy.disabled_repositories
                or identity.reviewer_id not in policy.reviewer_ids
            ):
                raise ReviewConflict("Repository or reviewer is disabled by review policy")
            active = await db.scalar(
                select(AIRun).where(
                    AIRun.review_task_id == task.id, AIRun.status.in_(["queued", "running"])
                )
            )
            if active is not None:
                if active.analysis_key != identity.key:
                    raise ReviewConflict("Another analysis is already queued or running")
                return record(active)
            held = await db.scalar(
                select(RunClaim).join(AIRun).where(AIRun.review_task_id == task.id)
            )
            if held is not None:
                raise ReviewConflict("Previous worker has not released ownership")
            conversation = await db.scalar(
                select(ReviewSession)
                .where(
                    ReviewSession.review_task_id == task.id,
                    ReviewSession.reviewer_id == identity.reviewer_id,
                )
                .order_by(ReviewSession.generation.desc())
                .limit(1)
            )
            if (
                conversation is not None
                and conversation.cleanup_status != "not_requested"
                and conversation.status != "cleaned"
            ):
                raise ReviewConflict("Session cleanup is pending")
            if conversation is None or conversation.status == "cleaned":
                conversation = ReviewSession(
                    review_task_id=task.id,
                    reviewer_id=identity.reviewer_id,
                    generation=conversation.generation + 1 if conversation else 1,
                    display_name=f"{repo.namespace}/{repo.name}#{pr.number}",
                )
                db.add(conversation)
                await db.flush()
            attempt = await db.scalar(
                select(func.max(AIRun.attempt)).where(AIRun.analysis_key == identity.key)
            )
            run = AIRun(
                review_task_id=task.id,
                review_session_id=conversation.id,
                reviewer_id=identity.reviewer_id,
                head_sha=identity.head_sha,
                base_sha=str(monitor.snapshot_json["base_sha"]),
                prompt_version=identity.prompt_version,
                config_hash=identity.config_hash,
                analysis_key=identity.key,
                attempt=(attempt or 0) + 1,
            )
            db.add(run)
            await transition(db, task, "QUEUED")
            await db.flush()
            result = record(run)
            await db.commit()
            return result

    async def claim_review(
        self, run_id: UUID, *, capacity: int, policy: ReviewPolicy
    ) -> Claim | None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        async with self.database.sessions() as db:
            await self._begin(db)
            run = await self._run(db, run_id)
            if run.status != "queued":
                return None
            task, monitor = await self._task(db, run.review_task_id)
            if task.state != "QUEUED" or monitor.snapshot_json["head_sha"] != run.head_sha:
                raise ReviewConflict("Queued analysis is no longer current")
            pr = await db.get(PullRequest, task.pull_request_id)
            assert pr is not None
            repo = await db.get(Repository, pr.repository_id)
            assert repo is not None
            if (
                not repo.enabled
                or f"{repo.namespace}/{repo.name}" in policy.disabled_repositories
                or run.reviewer_id not in policy.reviewer_ids
            ):
                return None
            count = await db.scalar(select(func.count()).select_from(RunClaim))
            if (count or 0) >= capacity:
                return None
            claim = RunClaim(run_id=run.id)
            db.add(claim)
            run.status = "running"
            run.started_at = utc_now()
            await transition(db, task, "AI_RUNNING")
            await db.flush()
            result = Claim(claim.token, record(run))
            await db.commit()
            return result

    async def cancel_review(self, run_id: UUID) -> RunRecord:
        async with self.database.sessions() as db:
            await self._begin(db)
            run = await self._run(db, run_id)
            if run.status in {"queued", "running"}:
                run.status = "cancelled"
                run.finished_at = utc_now()
                task = await db.get(ReviewTask, run.review_task_id)
                assert task is not None
                if task.completed_at is None and task.state in {"QUEUED", "AI_RUNNING"}:
                    await transition(db, task, "PENDING_AI_REVIEW")
            result = record(run)
            await db.commit()
            return result

    async def release_review(self, token: UUID) -> RunRecord:
        async with self.database.sessions() as db:
            await self._begin(db)
            claim = await db.get(RunClaim, token)
            if claim is None:
                raise ReviewConflict("Worker claim does not exist")
            run = await self._run(db, claim.run_id)
            if run.status == "running":
                run.status = "failed"
                run.finished_at = utc_now()
                task = await db.get(ReviewTask, run.review_task_id)
                assert task is not None
                if task.completed_at is None and task.state == "AI_RUNNING":
                    await transition(db, task, "PENDING_HUMAN_REVIEW")
            await db.delete(claim)
            result = record(run)
            await db.commit()
            return result
