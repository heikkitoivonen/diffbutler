"""Short SQLite transactions serialize explicit starts, sync, and worker claims."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from diffbutler.domain.review import (
    AnalysisIdentity,
    ReviewConflict,
    ReviewPolicy,
    ReviewResultRecord,
    RunRecord,
    RunStatus,
    SessionRecord,
    SessionStatus,
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
    ReviewResult,
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


def session_record(session: ReviewSession) -> SessionRecord:
    return SessionRecord(
        session.id,
        session.review_task_id,
        session.reviewer_id,
        session.generation,
        session.display_name,
        session.backend_session_id,
        SessionStatus(session.status),
        session.created_at,
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

    async def _session(self, db: AsyncSession, session_id: UUID) -> ReviewSession:
        session = await db.get(ReviewSession, session_id)
        if session is None:
            raise ReviewConflict("Review session does not exist")
        return session

    async def get_session(self, session_id: UUID) -> SessionRecord:
        async with self.database.sessions() as db:
            return session_record(await self._session(db, session_id))

    async def bind_backend_session(self, token: UUID, backend_session_id: str) -> SessionRecord:
        async with self.database.sessions() as db:
            await self._begin(db)
            claim = await db.get(RunClaim, token)
            if claim is None:
                raise ReviewConflict("Worker claim does not exist")
            run = await self._run(db, claim.run_id)
            session = await self._session(db, run.review_session_id)
            self._bind_backend_session(session, backend_session_id)
            result = session_record(session)
            await db.commit()
            return result

    @staticmethod
    def _bind_backend_session(session: ReviewSession, backend_session_id: str) -> None:
        # Opaque identifiers may contain spaces or shell metacharacters. They are
        # data, never command templates or paths. Bound their stored size and reject
        # terminal/control characters without including the value in diagnostics.
        if (
            not backend_session_id.strip()
            or len(backend_session_id) > 1024
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in backend_session_id)
        ):
            raise ValueError("Backend session ID must be nonblank, bounded, and control-free")
        if session.status == "cleaned":
            raise ReviewConflict("Review session was cleaned")
        if session.backend_session_id not in {None, backend_session_id}:
            raise ReviewConflict("Backend session identity cannot be replaced")
        session.backend_session_id = backend_session_id

    async def suspend_session(self, session_id: UUID) -> SessionRecord:
        async with self.database.sessions() as db:
            await self._begin(db)
            session = await self._session(db, session_id)
            await self._task(db, session.review_task_id)
            if session.status == "cleaned" or session.cleanup_status != "not_requested":
                raise ReviewConflict("Review session is cleaned or awaiting cleanup")
            active = await db.scalar(
                select(AIRun.id).where(
                    AIRun.review_session_id == session.id,
                    AIRun.status.in_(["queued", "running"]),
                )
            )
            held = await db.scalar(
                select(RunClaim.token).join(AIRun).where(AIRun.review_session_id == session.id)
            )
            if active is not None or held is not None:
                raise ReviewConflict("Review session still has queued work or worker ownership")
            session.status = "suspended"
            result = session_record(session)
            await db.commit()
            return result

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
            session = await self._session(db, run.review_session_id)
            if session.status == "cleaned" or session.cleanup_status != "not_requested":
                raise ReviewConflict("Review session is cleaned or awaiting cleanup")
            session.status = "ready"
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

    async def get_result(self, run_id: UUID) -> ReviewResultRecord | None:
        async with self.database.sessions() as db:
            run = await self._run(db, run_id)
            result = await db.get(ReviewResult, run_id)
            if result is None:
                return None
            assert run.exit_code is not None
            return ReviewResultRecord(
                run.exit_code,
                run.stdout_blob,
                run.stderr_blob,
                result.document_json,
                result.parse_tier,
                result.error,
                tuple(result.notes),
                tuple(result.selected_indices),
                result.min_confidence,
                result.findings_threshold,
            )

    async def finalize_review(
        self, token: UUID, result: ReviewResultRecord, *, backend_session_id: str | None
    ) -> RunRecord:
        """Caller has established process-tree exit; persist and release atomically."""
        async with self.database.sessions() as db:
            await self._begin(db)
            claim = await db.get(RunClaim, token)
            if claim is None:
                raise ReviewConflict("Worker claim does not exist")
            run = await self._run(db, claim.run_id)
            session = await self._session(db, run.review_session_id)
            if session.status == "cleaned":
                raise ReviewConflict("Review session was cleaned")
            if backend_session_id is not None:
                self._bind_backend_session(session, backend_session_id)
            if await db.get(ReviewResult, run.id) is not None:
                raise ReviewConflict("Review result already exists")
            run.exit_code = result.exit_code
            run.stdout_blob = result.stdout
            run.stderr_blob = result.stderr
            run.parse_status = "succeeded" if result.document_json is not None else "failed"
            db.add(
                ReviewResult(
                    run_id=run.id,
                    document_json=result.document_json,
                    parse_tier=result.parse_tier,
                    error=result.error,
                    notes=list(result.notes),
                    selected_indices=list(result.selected_indices),
                    min_confidence=result.min_confidence,
                    findings_threshold=result.findings_threshold,
                )
            )
            if run.status == "running":
                task = await db.get(ReviewTask, run.review_task_id)
                assert task is not None
                monitor = await db.get(PRMonitor, (task.account_id, task.pull_request_id))
                current = (
                    task.completed_at is None
                    and task.state == "AI_RUNNING"
                    and monitor is not None
                    and monitor.task_id == task.id
                    and monitor.snapshot_json["state"] == "open"
                    and monitor.snapshot_json["head_sha"] == run.head_sha
                    and monitor.snapshot_json["base_sha"] == run.base_sha
                    and session.cleanup_status == "not_requested"
                )
                if current:
                    run.status = "succeeded" if result.document_json is not None else "failed"
                    state = "FINDINGS_TRIAGE" if result.selected_indices else "PENDING_HUMAN_REVIEW"
                    await transition(db, task, state)
                else:
                    run.status = "cancelled" if task.completed_at is not None else "stale"
                    # A changed base may not yet have invalidated queue attention.
                    if task.completed_at is None and task.state == "AI_RUNNING":
                        await transition(db, task, "PENDING_AI_REVIEW")
                run.finished_at = utc_now()
            await db.delete(claim)
            saved = record(run)
            await db.commit()
            return saved
