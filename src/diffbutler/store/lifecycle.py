"""Short transactions for foreground leases and completion-triggered cleanup."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from diffbutler.domain.lifecycle import CleanupClaim, CleanupSession, ForegroundLease, HeldOperation
from diffbutler.domain.review import ReviewConflict
from diffbutler.store.database import Database
from diffbutler.store.models import (
    AIRun,
    CleanupJob,
    PRMonitor,
    ReviewSession,
    ReviewTask,
    RunClaim,
    utc_now,
)
from diffbutler.store.models import CleanupClaim as ClaimModel
from diffbutler.store.models import ForegroundLease as LeaseModel


async def _background_owned(db: AsyncSession, task_id: UUID) -> bool:
    active = await db.scalar(
        select(AIRun.id).where(
            AIRun.review_task_id == task_id, AIRun.status.in_(("queued", "running"))
        )
    )
    held = await db.scalar(
        select(RunClaim.token).join(AIRun).where(AIRun.review_task_id == task_id)
    )
    return active is not None or held is not None


class SQLiteLifecycleStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def _begin(self, db: AsyncSession) -> None:
        connection = await db.connection()
        await connection.exec_driver_sql("BEGIN IMMEDIATE")

    async def held_operations(self) -> tuple[HeldOperation, ...]:
        """Expose durable ownership for recovery; ages are not evidence of exit."""
        async with self.database.sessions() as db:
            foreground = await db.scalars(select(LeaseModel).order_by(LeaseModel.acquired_at))
            result = [
                HeldOperation(row.token, "foreground", row.session_id, row.acquired_at)
                for row in foreground
            ]
            cleanup = await db.scalars(select(ClaimModel).order_by(ClaimModel.acquired_at))
            result.extend(
                HeldOperation(row.token, "cleanup", row.job_id, row.acquired_at) for row in cleanup
            )
            return tuple(result)

    async def acquire_foreground(self, session_id: UUID) -> ForegroundLease:
        async with self.database.sessions() as db:
            await self._begin(db)
            session = await db.get(ReviewSession, session_id)
            if (
                session is None
                or session.status == "cleaned"
                or session.cleanup_status != "not_requested"
            ):
                raise ReviewConflict("Session is missing, cleaned, or awaiting cleanup")
            task = await db.get(ReviewTask, session.review_task_id)
            if task is None or task.completed_at is not None:
                raise ReviewConflict("Session task is completed")
            monitor = await db.get(PRMonitor, (task.account_id, task.pull_request_id))
            if (
                monitor is None
                or monitor.task_id != task.id
                or monitor.snapshot_json["state"] != "open"
            ):
                raise ReviewConflict("Session task is no longer current and open")
            if not session.backend_session_id:
                raise ReviewConflict("Session has no resumable backend identity")
            if await _background_owned(db, task.id):
                raise ReviewConflict("Background work still owns this task")
            if await db.scalar(select(LeaseModel.token).where(LeaseModel.session_id == session.id)):
                raise ReviewConflict("Foreground process still owns this session")
            lease = LeaseModel(
                session_id=session.id,
                backend_session_id=session.backend_session_id,
                head_sha=str(monitor.snapshot_json["head_sha"]),
                task_version=task.action_version,
            )
            db.add(lease)
            session.status = "ready"
            session.resumed_at = utc_now()
            await db.flush()
            result = ForegroundLease(
                lease.token,
                session.id,
                lease.backend_session_id,
                lease.head_sha,
                lease.task_version,
                lease.acquired_at,
            )
            await db.commit()
            return result

    async def release_foreground(self, token: UUID) -> None:
        """Caller has established exit (or that launch never occurred), including descendants."""
        async with self.database.sessions() as db:
            await self._begin(db)
            lease = await db.get(LeaseModel, token)
            if lease is None:
                raise ReviewConflict("Foreground lease does not exist")
            session = await db.get(ReviewSession, lease.session_id)
            assert session is not None
            session.status = "suspended"
            await db.delete(lease)
            await db.commit()

    async def claim_cleanup(self) -> CleanupClaim | None:
        """Skip busy jobs; no timeout can steal ownership from a living process."""
        async with self.database.sessions() as db:
            await self._begin(db)
            jobs = await db.scalars(
                select(CleanupJob)
                .where(
                    CleanupJob.status.in_(("pending", "failed")),
                    (CleanupJob.next_retry_at.is_(None)) | (CleanupJob.next_retry_at <= utc_now()),
                )
                .order_by(CleanupJob.created_at)
            )
            for job in jobs:
                task = await db.get(ReviewTask, job.review_task_id)
                if task is None or task.completed_at is None:
                    continue
                if await _background_owned(db, task.id):
                    continue
                foreground = await db.scalar(
                    select(LeaseModel.token)
                    .join(ReviewSession)
                    .where(ReviewSession.review_task_id == task.id)
                )
                if foreground is not None:
                    continue
                sessions = tuple(
                    await db.scalars(
                        select(ReviewSession).where(
                            ReviewSession.review_task_id == task.id,
                            ReviewSession.status != "cleaned",
                        )
                    )
                )
                claim = ClaimModel(job_id=job.id)
                db.add(claim)
                job.status = "running"
                job.attempts += 1
                job.last_error = None
                for session in sessions:
                    session.cleanup_status = "running"
                await db.flush()
                result = CleanupClaim(
                    claim.token,
                    job.id,
                    tuple(
                        CleanupSession(
                            session.id,
                            session.reviewer_id,
                            session.backend_session_id,
                            session.transcript_ref,
                            tuple(
                                tuple(sorted(item.items()))
                                for item in session.resource_manifest_json
                            ),
                        )
                        for session in sessions
                    ),
                )
                await db.commit()
                return result
            return None

    async def finish_cleanup(self, token: UUID, *, succeeded: bool) -> None:
        """Commit deletion evidence, or retain resources for an idempotent retry."""
        async with self.database.sessions() as db:
            await self._begin(db)
            claim = await db.get(ClaimModel, token)
            if claim is None:
                raise ReviewConflict("Cleanup claim does not exist")
            job = await db.get(CleanupJob, claim.job_id)
            assert job is not None
            sessions = await db.scalars(
                select(ReviewSession).where(
                    ReviewSession.review_task_id == job.review_task_id,
                    ReviewSession.status != "cleaned",
                )
            )
            for session in sessions:
                session.cleanup_status = "succeeded" if succeeded else "failed"
                if succeeded:
                    session.status = "cleaned"
                    session.cleaned_at = utc_now()
                    session.backend_session_id = None
                    session.transcript_ref = None
                    session.resource_manifest_json = []
            if succeeded:
                runs = await db.scalars(
                    select(AIRun).where(AIRun.review_task_id == job.review_task_id)
                )
                for run in runs:
                    run.stdout_blob = run.stderr_blob = None
                job.status = "succeeded"
                job.completed_at = utc_now()
                job.next_retry_at = None
                job.last_error = None
            else:
                job.status = "failed"
                job.last_error = "Session cleanup failed; resources retained for retry"
                job.next_retry_at = utc_now() + timedelta(
                    seconds=min(3600, 30 * 2 ** min(job.attempts, 7))
                )
            await db.delete(claim)
            await db.commit()
