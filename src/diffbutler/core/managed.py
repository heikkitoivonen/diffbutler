"""Configured review workflow shared by CLI and dashboard."""

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from uuid import UUID

from diffbutler.config.settings import ConfigError, Settings, load_settings
from diffbutler.core.application import local_store
from diffbutler.core.context import assemble_context
from diffbutler.core.lifecycle import LifecycleService
from diffbutler.core.native import (
    NativeExecution,
    NativeSessionCleaner,
    SessionLaunch,
    ensure_session_directory,
    prepare_native_handoff,
)
from diffbutler.core.process import ProcessRecovery
from diffbutler.core.review import AIRun, ReviewResult, ReviewService
from diffbutler.core.sync import PullRequestRow
from diffbutler.core.worker import ReviewWorker
from diffbutler.domain.lifecycle import CleanupStatus, ForegroundExitUncertain
from diffbutler.domain.review import (
    AnalysisIdentity,
    ReviewConflict,
    ReviewPolicy,
    RunClaim,
    RunRecord,
)
from diffbutler.reviewers.base import ReviewBudget, ReviewContext
from diffbutler.reviewers.native import native_preset
from diffbutler.reviewers.process import LocalHostBoot
from diffbutler.reviewers.prompt import PROMPT_VERSION
from diffbutler.reviewers.resources import OwnedDirectories, settled_io
from diffbutler.sources.fetch import fetch_repository
from diffbutler.sources.git import GitSourceReader, SourceError
from diffbutler.store.dashboard import SQLiteDashboardStore
from diffbutler.store.database import Database
from diffbutler.store.lifecycle import SQLiteLifecycleStore
from diffbutler.store.process import SQLiteProcessStore
from diffbutler.store.queue import SQLiteQueueStore
from diffbutler.store.review import SQLiteReviewStore


async def resolve_local(store: SQLiteQueueStore, value: str, login: str | None) -> PullRequestRow:
    matches: list[PullRequestRow] = []
    for account in await store.accounts():
        if login is not None and account.login != login:
            continue
        for tracked in (await store.snapshot(account)).items:
            pr = tracked.pull_request
            if value in {pr.url, f"{pr.ref.namespace}/{pr.ref.repository}#{pr.ref.number}"}:
                matches.append(PullRequestRow(account=account, tracked=tracked))
    if len(matches) != 1:
        raise ReviewConflict(
            "PR is missing or ambiguous in the local queue; sync/follow or select --login"
        )
    return matches[0]


async def owned_directories(settings: Settings) -> OwnedDirectories:
    path = settings.general.workspace_root
    if path.exists():
        return await OwnedDirectories.reopen(path)
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    try:
        return await OwnedDirectories.initialize(path)
    except FileExistsError:
        return await OwnedDirectories.reopen(path)


class ManagedReviews:
    def __init__(
        self, database: Database, settings: Settings, directories: OwnedDirectories
    ) -> None:
        self.settings = settings
        self.directories = directories
        self.store = SQLiteReviewStore(database)
        self.leases = SQLiteLifecycleStore(database)
        self.processes = SQLiteProcessStore(database)
        self.recovery = ProcessRecovery(self.processes, self.store, self.leases, LocalHostBoot())
        self.reviews = ReviewService(
            self.store,
            ReviewPolicy(
                frozenset(item.id for item in settings.reviewers),
                frozenset(key for key, repo in settings.repos.items() if not repo.enabled),
            ),
        )
        self.lifecycle = LifecycleService(
            self.leases, NativeSessionCleaner(directories), prepare_cleanup=self._prepare_cleanup
        )

    async def _prepare_cleanup(self, token: UUID) -> None:
        await self.recovery.prepare_launch(token, "cleanup")

    async def cleanup(self) -> CleanupStatus:
        await self.recovery.recover_rebooted()
        while await self.lifecycle.cleanup_once():
            pass
        return await self.leases.cleanup_status()

    def reviewer_command(self, reviewer_id: str | None) -> tuple[str, ...]:
        selected = next((item for item in self.settings.reviewers if item.id == reviewer_id), None)
        if selected is None or selected.preset not in {"codex", "claude"}:
            raise ConfigError(
                "Native execution requires a reviewer with preset = 'codex' or 'claude'"
            )
        return tuple(selected.command)

    def reviewer_preset(self, reviewer_id: str | None) -> Literal["codex", "claude"]:
        self.reviewer_command(reviewer_id)
        selected = next(item for item in self.settings.reviewers if item.id == reviewer_id)
        assert selected.preset is not None
        return selected.preset

    async def review(
        self,
        row: PullRequestRow,
        reviewer_id: str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> AIRun:
        task_id = row.tracked.task_id
        if task_id is None:
            raise ReviewConflict("PR has no persisted review task")
        pr = row.tracked.pull_request
        repository = self.settings.repos.get(f"{pr.ref.namespace}/{pr.ref.repository}")
        reviewer_id = reviewer_id or self.settings.review.default_reviewer
        command = self.reviewer_command(reviewer_id)
        selected = next(item for item in self.settings.reviewers if item.id == reviewer_id)
        instructions = "Inspect pinned base/ and head/ sources; do not execute project code."
        if self.settings.review.prompt is not None:
            instructions += "\n" + await asyncio.to_thread(self.settings.review.prompt.read_text)
        digest = hashlib.sha256(
            (
                selected.model_dump_json() + self.settings.review.model_dump_json() + instructions
            ).encode()
        ).hexdigest()
        identity = AnalysisIdentity(task_id, pr.head_sha, selected.id, PROMPT_VERSION, digest)
        run = await self.reviews.request_review(identity)
        repository_path = repository.path if repository else None
        sources: GitSourceReader | None = None

        def report(message: str) -> None:
            if progress is not None:
                progress(message)

        report("Review queued")

        async def before_prepare(claim: RunClaim) -> None:
            nonlocal repository_path, sources
            # Fail before fetching or snapshotting if safe launch tracking is unavailable.
            await self.recovery.host.current()
            report("Preparing repository…")
            if repository_path is not None:
                try:
                    sources = await GitSourceReader.open(
                        repository_path, base_sha=claim.run.base_sha, head_sha=claim.run.head_sha
                    )
                    return
                except SourceError:
                    # Fetch into our own session, never mutate the user's checkout.
                    pass
            root = await ensure_session_directory(
                self.store, self.directories, claim, self.reviewer_preset(reviewer_id)
            )
            destination = root / f"source-{claim.run.id}.git"
            repository_path = destination
            await settled_io(lambda: destination.mkdir(mode=0o700, exist_ok=True))
            report("Fetching PR commits…")
            await fetch_repository(repository_path, pr.ref, claim.run.base_sha, claim.run.head_sha)

        async def prepare(record: RunRecord) -> ReviewContext:
            nonlocal sources
            assert repository_path is not None
            if sources is None:
                sources = await GitSourceReader.open(
                    repository_path, base_sha=record.base_sha, head_sha=record.head_sha
                )
            report("Preparing AI review…")
            return await assemble_context(
                run=record,
                pr=pr,
                sources=sources,
                instructions=instructions,
                schema_json="{}",
                budget=ReviewBudget(
                    selected.timeout_s,
                    self.settings.review.max_diff_bytes,
                    selected.max_output_bytes,
                ),
            )

        def execution(claim: RunClaim) -> NativeExecution:
            assert sources is not None
            return NativeExecution(
                claim,
                self.store,
                self.directories,
                sources,
                command,
                self.recovery,
                self.processes,
                lambda: self.reviews.claim_is_current(claim.token),
                self.reviewer_preset(reviewer_id),
            )

        worker = ReviewWorker(
            self.reviews,
            prepare,
            execution,
            before_prepare=before_prepare,
            output_format=native_preset(self.reviewer_preset(reviewer_id)).output_format,
            response_format="markdown",
            min_confidence=self.settings.review.min_confidence,
            findings_threshold=self.settings.review.findings_threshold,
        )
        try:
            while True:
                preferences = await SQLiteDashboardStore(self.store.database).preferences()
                result = await worker.execute_requested(
                    run.id,
                    capacity=self.settings.general.max_concurrent_reviews,
                    paused=preferences.get("reviews_paused") == "true",
                )
                if result is not None:
                    await self.cleanup()
                    return result
                current = await self.reviews.get_run(run.id)
                if current.status != "queued":
                    return current
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            await self.reviews.cancel_review(run.id)
            raise

    async def findings(self, row: PullRequestRow) -> ReviewResult | None:
        if row.tracked.task_id is None:
            return None
        run = await self.store.latest_run(row.tracked.task_id)
        return None if run is None else await self.reviews.get_result(run.id)

    async def prepare_handoff(self, row: PullRequestRow) -> SessionLaunch:
        if (
            row.tracked.task_id is None
            or (run := await self.store.latest_run(row.tracked.task_id)) is None
        ):
            raise ReviewConflict("PR has no saved native review session")
        return await prepare_native_handoff(
            self.store,
            self.leases,
            self.directories,
            run.session_id,
            self.reviewer_command(run.reviewer_id),
            self.reviewer_preset(run.reviewer_id),
        )


async def review_configured(
    path: Path | None,
    value: str,
    login: str | None,
    reviewer: str | None,
    progress: Callable[[str], None] | None = None,
) -> AIRun:
    async with local_store(path) as (queue, _settings):
        row = await resolve_local(queue, value, login)
        ref = row.tracked.pull_request.ref
        settings = await asyncio.to_thread(
            load_settings, path, repo=f"{ref.namespace}/{ref.repository}"
        )
        managed = ManagedReviews(queue.database, settings, await owned_directories(settings))
        await managed.cleanup()
        return await managed.review(row, reviewer, progress)


async def findings_configured(
    path: Path | None, value: str, login: str | None
) -> ReviewResult | None:
    async with local_store(path) as (queue, _settings):
        row = await resolve_local(queue, value, login)
        # Local history reads do not create filesystem workspaces or invoke native tools.
        store = SQLiteReviewStore(queue.database)
        if (
            row.tracked.task_id is None
            or (run := await store.latest_run(row.tracked.task_id)) is None
        ):
            return None
        return await ReviewService(store, ReviewPolicy(frozenset())).get_result(run.id)


async def cleanup_configured(path: Path | None) -> CleanupStatus:
    async with local_store(path) as (queue, settings):
        managed = ManagedReviews(queue.database, settings, await owned_directories(settings))
        return await managed.cleanup()


async def approved_configured(path: Path | None, value: str, login: str | None) -> CleanupStatus:
    async with local_store(path) as (queue, settings):
        row = await resolve_local(queue, value, login)
        if row.tracked.task_id is None:
            raise ReviewConflict("PR has no persisted review task")
        managed = ManagedReviews(queue.database, settings, await owned_directories(settings))
        await managed.store.mark_approved(row.tracked.task_id)
        return await managed.cleanup()


@asynccontextmanager
async def handoff_configured(
    path: Path | None, value: str, login: str | None
) -> AsyncGenerator[tuple[SessionLaunch, Callable[[int], Awaitable[None]]]]:
    """Own the session lifetime; the UI attaches and verifies foreground exit."""
    async with local_store(path) as (queue, settings):
        row = await resolve_local(queue, value, login)
        managed = ManagedReviews(queue.database, settings, await owned_directories(settings))
        await managed.cleanup()
        launch = await managed.prepare_handoff(row)
        exited = True
        try:
            assert row.tracked.task_id is not None
            await SQLiteDashboardStore(queue.database).refresh_pending(row.tracked.task_id)
            await managed.recovery.prepare_launch(launch.token, "foreground")

            async def started(pid: int) -> None:
                await managed.processes.bind_launcher(launch.token, pid)

            yield launch, started
        except ForegroundExitUncertain:
            exited = False
            raise
        finally:
            if exited:
                await managed.lifecycle.release_foreground(launch.token)
                from diffbutler.core.dashboard import refresh_configured
                from diffbutler.forges.base import ForgeError

                # Durable refresh_pending remains visible and polling retries reads only.
                with suppress(ForgeError, ConfigError):
                    await refresh_configured(path, value, login)
                await managed.cleanup()
