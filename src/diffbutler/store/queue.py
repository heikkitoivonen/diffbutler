"""SQLite queue repository. Network reads happen before short atomic commits."""

from typing import cast

from pydantic import TypeAdapter
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from diffbutler.domain.forge import Account as ForgeIdentity
from diffbutler.domain.forge import PRRef
from diffbutler.domain.forge import PullRequest as ForgePR
from diffbutler.domain.sync import QueueSnapshot, TrackedPR
from diffbutler.store.base import SyncConflict
from diffbutler.store.database import Database
from diffbutler.store.models import (
    Account,
    AIRun,
    CleanupJob,
    ForgeAccount,
    PRMonitor,
    PullRequest,
    Repository,
    ReviewTask,
    utc_now,
)

PR_ADAPTER = TypeAdapter(ForgePR)


class SQLiteQueueStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def accounts(self) -> tuple[ForgeIdentity, ...]:
        async with self.database.sessions() as session:
            rows = await session.execute(select(ForgeAccount, Account).join(Account))
            return tuple(
                ForgeIdentity(identity.forge, identity.login, account.display_name)
                for identity, account in rows.tuples()
            )

    async def snapshot(self, account: ForgeIdentity) -> QueueSnapshot:
        async with self.database.sessions() as session:
            # One read transaction pairs the generation with all its monitor rows.
            await session.execute(text("BEGIN"))
            identity = await session.scalar(
                select(ForgeAccount).where(
                    ForgeAccount.forge == account.forge, ForgeAccount.login == account.login.lower()
                )
            )
            if identity is None:
                return QueueSnapshot(0, ())
            rows = await session.execute(
                select(PRMonitor, ReviewTask)
                .join(ReviewTask, PRMonitor.task_id == ReviewTask.id)
                .where(PRMonitor.account_id == identity.account_id)
            )
            return QueueSnapshot(
                identity.version,
                tuple(
                    TrackedPR(
                        PR_ADAPTER.validate_python(monitor.snapshot_json),
                        monitor.following,
                        tuple(monitor.sources),
                        task.cycle_number,
                        task.id,
                        task.state,
                        monitor.request_key,
                        monitor.review_key,
                        task.completion_reason,
                        task.completion_event_watermark,
                    )
                    for monitor, task in rows.tuples()
                ),
            )

    async def apply(
        self,
        account: ForgeIdentity,
        version: int,
        items: tuple[TrackedPR, ...],
        *,
        refreshed: frozenset[PRRef] = frozenset(),
    ) -> None:
        async with self.database.sessions() as session:
            connection = await session.connection()
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            identity = await session.scalar(
                select(ForgeAccount).where(
                    ForgeAccount.forge == account.forge, ForgeAccount.login == account.login.lower()
                )
            )
            if identity is None:
                if version != 0:
                    raise SyncConflict()
                ledger = Account(
                    forge=account.forge.split(":", 1)[0],
                    login=account.login.lower(),
                    display_name=account.display_name,
                )
                session.add(ledger)
                await session.flush()
                identity = ForgeAccount(
                    account_id=ledger.id,
                    forge=account.forge,
                    login=account.login.lower(),
                    version=0,
                )
                session.add(identity)
            if identity.version != version:
                raise SyncConflict()
            for item in items:
                if item.pull_request.ref.forge != account.forge:
                    raise ValueError("Cannot persist a PR belonging to a different account forge")
                await self._save(
                    session, identity, item, refresh=item.pull_request.ref in refreshed
                )
            identity.version += 1
            await session.commit()

    async def _save(
        self,
        session: AsyncSession,
        identity: ForgeAccount,
        item: TrackedPR,
        *,
        refresh: bool,
    ) -> None:
        remote = item.pull_request
        ref = remote.ref
        repo = await session.scalar(
            select(Repository).where(
                Repository.forge == ref.forge,
                Repository.namespace == ref.namespace,
                Repository.name == ref.repository,
            )
        )
        if repo is None:
            # The PR base branch is not necessarily the repository default branch.
            repo = Repository(
                forge=ref.forge, namespace=ref.namespace, name=ref.repository, default_branch=""
            )
            session.add(repo)
            await session.flush()
        pr = await session.scalar(
            select(PullRequest).where(
                PullRequest.repository_id == repo.id, PullRequest.number == ref.number
            )
        )
        if pr is None:
            refresh = True
            pr = PullRequest(repository_id=repo.id, number=ref.number)
            session.add(pr)
        if refresh:
            for name in (
                "title",
                "author",
                "url",
                "state",
                "head_sha",
                "base_sha",
                "head_ref",
                "base_ref",
                "is_draft",
                "created_at",
                "updated_at",
            ):
                setattr(pr, name, getattr(remote, name))
            pr.fetched_at = utc_now()
        await session.flush()
        monitor = await session.get(PRMonitor, (identity.account_id, pr.id))
        task = await session.get(ReviewTask, item.task_id) if item.task_id else None
        if task is None:
            task = ReviewTask(
                pull_request_id=pr.id, account_id=identity.account_id, cycle_number=item.cycle
            )
            session.add(task)
            await session.flush()
        if task.account_id != identity.account_id or task.pull_request_id != pr.id:
            raise ValueError("Review task belongs to a different account or PR")
        if monitor is not None and (
            item.state == "DONE"
            or remote.head_sha != monitor.snapshot_json.get("head_sha")
            or item.request_key > monitor.request_key
        ):
            # Invalidate analysis in the same commit as the observed change. A
            # worker's separate claim remains held until it acknowledges exit.
            runs = await session.scalars(
                select(AIRun).where(
                    AIRun.review_task_id == monitor.task_id,
                    AIRun.status.in_(["queued", "running"]),
                )
            )
            for run in runs:
                run.status = "cancelled" if item.state == "DONE" else "stale"
                run.finished_at = utc_now()
        if task.completed_at is None:
            if task.state != item.state:
                task.action_version += 1
                task.updated_at = utc_now()
            task.state = item.state
            task.following = item.following
            task.request_sources_json = list(item.sources)
            task.last_request_event = item.request_key or None
            task.last_activity_cursor = item.review_key or None
        if item.state == "DONE" and task.completed_at is None:
            task.completed_at = utc_now()
            task.completed_head_sha = remote.head_sha
            task.completion_reason = item.completion_reason
            task.completion_source = "forge"
            task.completion_event_watermark = item.completion_event
            task.approved_sha = (
                remote.head_sha if item.completion_reason == "own_approval" else None
            )
            session.add(CleanupJob(review_task_id=task.id))
        if monitor is None:
            monitor = PRMonitor(account_id=identity.account_id, pull_request_id=pr.id)
            session.add(monitor)
        monitor.task_id = task.id
        monitor.following = item.following
        monitor.sources = list(item.sources)
        monitor.snapshot_json = cast(dict[str, object], PR_ADAPTER.dump_python(remote, mode="json"))
        monitor.request_key = item.request_key
        monitor.review_key = item.review_key
