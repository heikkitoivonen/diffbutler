"""Configured M2 workflow shared by CLI and future dashboard."""

import asyncio
import hashlib
from pathlib import Path

from diffbutler.config.settings import ConfigError, Settings, load_settings
from diffbutler.core.application import local_store
from diffbutler.core.context import assemble_context
from diffbutler.core.lifecycle import LifecycleService
from diffbutler.core.native import (
    NativeExecution,
    NativeSessionCleaner,
    SessionLaunch,
    prepare_native_handoff,
)
from diffbutler.core.process import ProcessRecovery
from diffbutler.core.review import AIRun, ReviewResult, ReviewService
from diffbutler.core.sync import PullRequestRow
from diffbutler.core.worker import ReviewWorker
from diffbutler.domain.review import (
    AnalysisIdentity,
    ReviewConflict,
    ReviewPolicy,
    RunClaim,
    RunRecord,
)
from diffbutler.reviewers.base import ReviewBudget, ReviewContext
from diffbutler.reviewers.native import CODEX_OUTPUT
from diffbutler.reviewers.process import LinuxHostBoot
from diffbutler.reviewers.prompt import PROMPT_VERSION
from diffbutler.reviewers.resources import OwnedDirectories
from diffbutler.sources.git import GitSourceReader
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
        self.recovery = ProcessRecovery(self.processes, self.store, self.leases, LinuxHostBoot())
        self.reviews = ReviewService(
            self.store,
            ReviewPolicy(
                frozenset(item.id for item in settings.reviewers),
                frozenset(key for key, repo in settings.repos.items() if not repo.enabled),
            ),
        )
        self.lifecycle = LifecycleService(self.leases, NativeSessionCleaner(directories))

    async def cleanup(self) -> None:
        await self.recovery.recover_rebooted()
        while await self.lifecycle.cleanup_once():
            pass

    def reviewer_command(self, reviewer_id: str | None) -> tuple[str, ...]:
        selected = next((item for item in self.settings.reviewers if item.id == reviewer_id), None)
        if selected is None or selected.preset != "codex":
            raise ConfigError(
                "M2 native execution requires a configured reviewer with preset = 'codex'"
            )
        return tuple(selected.command)

    async def review(self, row: PullRequestRow, reviewer_id: str | None = None) -> AIRun:
        task_id = row.tracked.task_id
        if task_id is None:
            raise ReviewConflict("PR has no persisted review task")
        pr = row.tracked.pull_request
        repository = self.settings.repos.get(f"{pr.ref.namespace}/{pr.ref.repository}")
        if repository is None or repository.path is None:
            raise ConfigError(
                "Native review requires repos.<owner/repo>.path with pinned local Git objects"
            )
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
        repository_path = repository.path
        sources: GitSourceReader | None = None

        async def prepare(record: RunRecord) -> ReviewContext:
            nonlocal sources
            sources = await GitSourceReader.open(
                repository_path, base_sha=record.base_sha, head_sha=record.head_sha
            )
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
            )

        worker = ReviewWorker(
            self.reviews,
            prepare,
            execution,
            output_format=CODEX_OUTPUT,
            min_confidence=self.settings.review.min_confidence,
            findings_threshold=self.settings.review.findings_threshold,
        )
        while True:
            result = await worker.execute_requested(
                run.id, capacity=self.settings.general.max_concurrent_reviews
            )
            if result is not None:
                await self.cleanup()
                return result
            current = await self.reviews.get_run(run.id)
            if current.status != "queued":
                return current
            await asyncio.sleep(0.25)

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
        )


async def review_configured(
    path: Path | None, value: str, login: str | None, reviewer: str | None
) -> AIRun:
    async with local_store(path) as (queue, _settings):
        row = await resolve_local(queue, value, login)
        ref = row.tracked.pull_request.ref
        settings = await asyncio.to_thread(
            load_settings, path, repo=f"{ref.namespace}/{ref.repository}"
        )
        managed = ManagedReviews(queue.database, settings, await owned_directories(settings))
        await managed.cleanup()
        return await managed.review(row, reviewer)


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


async def cleanup_configured(path: Path | None) -> None:
    async with local_store(path) as (queue, settings):
        managed = ManagedReviews(queue.database, settings, await owned_directories(settings))
        await managed.cleanup()


async def approved_configured(path: Path | None, value: str, login: str | None) -> None:
    async with local_store(path) as (queue, settings):
        row = await resolve_local(queue, value, login)
        if row.tracked.task_id is None:
            raise ReviewConflict("PR has no persisted review task")
        managed = ManagedReviews(queue.database, settings, await owned_directories(settings))
        await managed.store.mark_approved(row.tracked.task_id)
        await managed.cleanup()
