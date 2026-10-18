"""Atomic local actions and offline history projections."""

from datetime import UTC
from uuid import UUID

from sqlalchemy import select, text

from diffbutler.domain.dashboard import CycleRecord
from diffbutler.domain.forge import Account
from diffbutler.domain.review import ReviewConflict
from diffbutler.store.database import Database
from diffbutler.store.models import (
    AIRun,
    CleanupJob,
    ForgeAccount,
    ObservedActivity,
    PRMonitor,
    ReviewSession,
    ReviewTask,
    UIPreference,
    utc_now,
)
from diffbutler.store.queue import EVENT_ADAPTER, PR_ADAPTER
from diffbutler.store.review import record, session_record, transition


class SQLiteDashboardStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def reset_task(self, task_id: UUID) -> None:
        """Forget active monitoring and invalidate work, retaining history and cleanup ownership."""
        async with self.database.sessions() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            task = await db.get(ReviewTask, task_id)
            if task is None or task.completed_at is not None:
                raise ReviewConflict("Review task is missing or completed")
            monitor = await db.get(PRMonitor, (task.account_id, task.pull_request_id))
            if monitor is None or monitor.task_id != task.id:
                raise ReviewConflict("Review task is no longer current")
            # Older migrated cycles may still rely on the monitor for their snapshot.
            cycles = await db.scalars(
                select(ReviewTask).where(
                    ReviewTask.account_id == task.account_id,
                    ReviewTask.pull_request_id == task.pull_request_id,
                )
            )
            for cycle in cycles:
                if not cycle.snapshot_json:
                    cycle.snapshot_json = monitor.snapshot_json
            await transition(db, task, "DONE")
            task.completed_at = utc_now()
            task.completion_reason = "local_reset"
            task.completion_source = "local"
            task.completed_head_sha = str(monitor.snapshot_json["head_sha"])
            task.following = False
            task.request_sources_json = []
            runs = await db.scalars(
                select(AIRun).where(
                    AIRun.review_task_id == task_id, AIRun.status.in_(("queued", "running"))
                )
            )
            for run in runs:
                run.status = "cancelled"
                run.status_reason = "PR reset locally"
                run.finished_at = utc_now()
            # Claims remain held until their supervisors establish process exit.
            db.add(CleanupJob(review_task_id=task_id))
            await db.delete(monitor)
            await db.commit()

    async def choose_state(self, task_id: UUID, *, waiting: bool) -> None:
        async with self.database.sessions() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
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
            now = utc_now()
            await transition(
                db, task, "PENDING_AUTHOR_CHANGES" if waiting else "PENDING_HUMAN_REVIEW"
            )
            # Events predating this explicit choice cannot override it on a retry.
            task.state_event_watermark = max(
                task.state_event_watermark or "",
                monitor.review_key,
                now.isoformat(timespec="microseconds") + ":~",
            )
            if not waiting:
                task.opened_at = now
            monitor.notice = (
                "Waiting for author chosen locally" if waiting else "Manual review chosen locally"
            )
            runs = await db.scalars(
                select(AIRun).where(
                    AIRun.review_task_id == task_id, AIRun.status.in_(("queued", "running"))
                )
            )
            for run in runs:
                run.status = "cancelled"
                run.finished_at = now
            await db.commit()

    async def refresh_pending(self, task_id: UUID) -> None:
        async with self.database.sessions() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            task = await db.get(ReviewTask, task_id)
            if task is None:
                raise ReviewConflict("Task does not exist")
            monitor = await db.get(PRMonitor, (task.account_id, task.pull_request_id))
            identity = await db.get(ForgeAccount, task.account_id)
            if monitor and identity:
                monitor.refresh_pending = True
                identity.version += 1
            await db.commit()

    async def preferences(self) -> dict[str, str]:
        async with self.database.sessions() as db:
            return {row.key: row.value for row in await db.scalars(select(UIPreference))}

    async def set_preference(self, key: str, value: str) -> None:
        async with self.database.sessions() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            row = await db.get(UIPreference, key)
            if row is None:
                db.add(UIPreference(key=key, value=value))
            else:
                row.value = value
            await db.commit()

    async def cycles(self) -> tuple[CycleRecord, ...]:
        async with self.database.sessions() as db:
            await db.execute(text("BEGIN"))
            rows = await db.execute(
                select(ReviewTask, ForgeAccount, PRMonitor)
                .join(ForgeAccount, ReviewTask.account_id == ForgeAccount.account_id)
                .outerjoin(
                    PRMonitor,
                    (PRMonitor.account_id == ReviewTask.account_id)
                    & (PRMonitor.pull_request_id == ReviewTask.pull_request_id),
                )
                .order_by(
                    ReviewTask.first_seen_at.desc(), ReviewTask.cycle_number.desc(), ReviewTask.id
                )
            )
            result: list[CycleRecord] = []
            for task, identity, monitor in rows:
                runs = tuple(
                    record(run)
                    for run in await db.scalars(
                        select(AIRun)
                        .where(AIRun.review_task_id == task.id)
                        .order_by(AIRun.requested_at, AIRun.attempt)
                    )
                )
                sessions = tuple(
                    session_record(row)
                    for row in await db.scalars(
                        select(ReviewSession)
                        .where(ReviewSession.review_task_id == task.id)
                        .order_by(ReviewSession.created_at)
                    )
                )
                receipts = tuple(
                    EVENT_ADAPTER.validate_json(row.payload_json)
                    for row in await db.scalars(
                        select(ObservedActivity)
                        .where(ObservedActivity.review_task_id == task.id)
                        .order_by(ObservedActivity.event_id)
                    )
                )
                cleanup = await db.scalar(
                    select(CleanupJob).where(CleanupJob.review_task_id == task.id)
                )
                result.append(
                    CycleRecord(
                        task.id,
                        Account(identity.forge, identity.login, identity.login),
                        PR_ADAPTER.validate_python(
                            task.snapshot_json or (monitor.snapshot_json if monitor else {})
                        ),
                        task.cycle_number,
                        task.state,
                        task.completed_at.replace(tzinfo=UTC) if task.completed_at else None,
                        task.completion_reason,
                        task.completed_head_sha,
                        cleanup.status if cleanup else "not_requested",
                        cleanup.last_error if cleanup else None,
                        task.opened_at.replace(tzinfo=UTC) if task.opened_at else None,
                        runs,
                        sessions,
                        receipts,
                    )
                )
            return tuple(result)
