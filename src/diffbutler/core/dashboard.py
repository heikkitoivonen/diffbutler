"""Shared dashboard projections, ordering, history, and explicit state actions."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from diffbutler.core.application import local_store, remote_service, sync_configured
from diffbutler.core.managed import resolve_local
from diffbutler.core.review import ReviewResult, ReviewService
from diffbutler.core.sync import PullRequestRow, list_queue
from diffbutler.domain.dashboard import COLUMN_IDS, STATE_LABELS, CycleRecord
from diffbutler.domain.review import ReviewConflict, ReviewPolicy
from diffbutler.forges.base import ForgeError
from diffbutler.store.base import ReviewStore
from diffbutler.store.dashboard import SQLiteDashboardStore
from diffbutler.store.review import SQLiteReviewStore


class DashboardStore(Protocol):
    async def cycles(self) -> tuple[CycleRecord, ...]: ...
    async def choose_state(self, task_id: UUID, *, waiting: bool) -> None: ...


class HistoryFilter(BaseModel):
    repository: str | None = None
    number: int | None = None
    reason: str | None = None
    login: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    completed: bool | None = True


class CycleDetail(BaseModel):
    model_config = ConfigDict(frozen=True)
    cycle: CycleRecord
    results: tuple[ReviewResult, ...]


class DashboardRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    row: PullRequestRow
    run_status: str = ""
    reviewed_sha: str = ""
    findings: int | None = None
    max_severity: str = ""
    unverified: int = 0
    session_name: str = ""
    attention: float = 0


class SortSpec(BaseModel):
    column: str = "attention"
    descending: bool = True


class DashboardService:
    def __init__(self, store: DashboardStore, reviews: ReviewStore) -> None:
        self.store = store
        self.reviews = ReviewService(reviews, ReviewPolicy(frozenset()))

    async def history(self, filters: HistoryFilter) -> tuple[CycleRecord, ...]:
        def matches(cycle: CycleRecord) -> bool:
            pr = cycle.pull_request
            return (
                (filters.completed is None or (cycle.completed_at is not None) == filters.completed)
                and (
                    filters.repository is None
                    or filters.repository == f"{pr.ref.namespace}/{pr.ref.repository}"
                )
                and (filters.number is None or filters.number == pr.ref.number)
                and (filters.reason is None or filters.reason == cycle.completion_reason)
                and (filters.login is None or filters.login.lower() == cycle.account.login.lower())
                and (
                    filters.since is None
                    or (cycle.completed_at is not None and cycle.completed_at >= filters.since)
                )
                and (
                    filters.until is None
                    or (cycle.completed_at is not None and cycle.completed_at <= filters.until)
                )
            )

        return tuple(cycle for cycle in await self.store.cycles() if matches(cycle))

    async def detail(self, task_id: UUID) -> CycleDetail:
        cycle = next((item for item in await self.store.cycles() if item.task_id == task_id), None)
        if cycle is None:
            raise ReviewConflict("History cycle does not exist")
        results: list[ReviewResult] = []
        for run in cycle.runs:
            result = await self.reviews.get_result(run.id)
            if result is not None:
                # Normal history never loads raw terminal output into a UI.
                results.append(result.model_copy(update={"stdout": None, "stderr": None}))
        return CycleDetail(cycle=cycle, results=tuple(results))

    async def continue_manual_review(self, task_id: UUID) -> None:
        await self.store.choose_state(task_id, waiting=False)

    async def mark_waiting_for_author(self, task_id: UUID) -> None:
        await self.store.choose_state(task_id, waiting=True)


SEVERITIES = {"question": 0, "nit": 1, "minor": 2, "important": 3, "blocking": 4}


def column_value(row: DashboardRow, column: str) -> str | int | float | None:
    tracked = row.row.tracked
    pr = tracked.pull_request
    if column == "attention":
        return row.attention
    values: dict[str, str | int | float | None] = {
        "state": STATE_LABELS.get(tracked.state, tracked.state),
        "repo": f"{pr.ref.namespace}/{pr.ref.repository}",
        "number": pr.ref.number,
        "title": pr.title,
        "author": pr.author,
        "ai_status": row.run_status,
        "findings": row.findings,
        "max_severity": SEVERITIES.get(row.max_severity, -1),
        "age": (datetime.now(UTC) - pr.created_at).total_seconds(),
        "updated": pr.updated_at.timestamp(),
        "branch": pr.head_ref,
        "draft": int(pr.is_draft),
        "reviewers": ", ".join((*pr.requested_users, *pr.requested_teams)),
        "unverified": row.unverified,
        "url": pr.url,
        "additions": pr.additions,
        "deletions": pr.deletions,
        "files": pr.files,
        "labels": ", ".join(pr.labels),
        "ci": pr.ci,
    }
    if column not in values:
        raise ValueError("Unknown dashboard column")
    return values[column]


def sort_rows(rows: tuple[DashboardRow, ...], sort: SortSpec) -> tuple[DashboardRow, ...]:
    if sort.column not in COLUMN_IDS:
        raise ValueError("Unknown dashboard sort column")
    present = [row for row in rows if column_value(row, sort.column) is not None]
    missing = [row for row in rows if column_value(row, sort.column) is None]

    def key(row: DashboardRow) -> str | int | float:
        value = column_value(row, sort.column)
        assert value is not None
        return value

    return tuple(sorted(present, key=key, reverse=sort.descending) + missing)


async def dashboard_rows(
    path: Path | None, view: str = "active", text: str = ""
) -> tuple[DashboardRow, ...]:
    async with local_store(path) as (queue, settings):
        selected: Literal["active", "requested", "following"] = (
            "requested" if view == "requested" else "following" if view == "following" else "active"
        )
        rows = await list_queue(queue, view=selected)
        store = SQLiteReviewStore(queue.database)
        reviews = ReviewService(store, ReviewPolicy(frozenset()))
        result: list[DashboardRow] = []
        for row in rows:
            if row.tracked.state == "DONE":
                continue
            if view == "triage" and row.tracked.state != "FINDINGS_TRIAGE":
                continue
            if view == "posted" and row.tracked.state != "PENDING_AUTHOR_CHANGES":
                continue
            pr = row.tracked.pull_request
            searchable = (
                f"{pr.ref.namespace}/{pr.ref.repository}#{pr.ref.number} {pr.title} {pr.author}"
            )
            if text.lower() not in searchable.lower():
                continue
            run = await store.latest_run(row.tracked.task_id) if row.tracked.task_id else None
            analysis = await reviews.get_result(run.id) if run else None
            session = await store.get_session(run.session_id) if run else None
            findings = analysis.document.findings if analysis and analysis.document else None
            result.append(
                DashboardRow(
                    row=row,
                    run_status=run.status if run else "",
                    reviewed_sha=run.head_sha if run else "",
                    session_name=session.display_name if session else "",
                    findings=len(findings) if findings is not None else None,
                    max_severity=max(
                        (finding.severity for finding in findings),
                        key=lambda value: SEVERITIES[value],
                        default="",
                    )
                    if findings
                    else "",
                    unverified=len(analysis.document.orientation.unverified)
                    if analysis and analysis.document
                    else 0,
                )
            )
        weights = settings.ui.attention

        def attention(row: DashboardRow) -> float:
            pr = row.row.tracked.pull_request
            severity = {
                "blocking": weights.blocking,
                "important": weights.important,
                "minor": 10,
            }.get(row.max_severity, 0)
            state = {"FINDINGS_TRIAGE": 50, "PENDING_HUMAN_REVIEW": 20}.get(
                row.row.tracked.state, 0
            )
            if row.run_status == "failed":
                state = 30
            age = max(0, (datetime.now(UTC) - pr.created_at).total_seconds() / 86400)
            size = ((pr.additions or 0) + (pr.deletions or 0)) / 100
            return (
                severity
                + state
                + age * weights.age_pressure_per_day
                + size * weights.size_penalty_per_100_lines
                - (20 if pr.is_draft else 0)
            )

        return tuple(row.model_copy(update={"attention": attention(row)}) for row in result)


async def history_configured(path: Path | None, filters: HistoryFilter) -> tuple[CycleRecord, ...]:
    async with local_store(path) as (queue, _):
        return await DashboardService(
            SQLiteDashboardStore(queue.database), SQLiteReviewStore(queue.database)
        ).history(filters)


async def detail_configured(path: Path | None, task_id: UUID) -> CycleDetail:
    async with local_store(path) as (queue, _):
        return await DashboardService(
            SQLiteDashboardStore(queue.database), SQLiteReviewStore(queue.database)
        ).detail(task_id)


async def state_configured(
    path: Path | None, value: str, login: str | None, *, waiting: bool
) -> str:
    async with local_store(path) as (queue, _):
        row = await resolve_local(queue, value, login)
        assert row.tracked.task_id is not None
        service = DashboardService(
            SQLiteDashboardStore(queue.database), SQLiteReviewStore(queue.database)
        )
        if waiting:
            await service.mark_waiting_for_author(row.tracked.task_id)
        else:
            await service.continue_manual_review(row.tracked.task_id)
        return row.tracked.pull_request.url


async def refresh_configured(path: Path | None, value: str, login: str | None) -> PullRequestRow:
    """Refresh one PR, preserving a durable pending indication on any failure."""
    async with local_store(path) as (queue, settings):
        row = await resolve_local(queue, value, login)
        assert row.tracked.task_id is not None
        await SQLiteDashboardStore(queue.database).refresh_pending(row.tracked.task_id)
        for index, config in enumerate(settings.forges, 1):
            if f"{config.kind}:{config.base_url.rstrip('/')}" != row.account.forge:
                continue
            if config.login and config.login.lower() != row.account.login.lower():
                continue
            async with remote_service(queue, settings, index) as service:
                if (await service.forge.whoami()).login.lower() != row.account.login.lower():
                    continue
                service.expected_login = row.account.login
                return await service.follow_pr(row.tracked.pull_request.ref)
        raise ForgeError("Refresh pending: no matching configured forge account")


class Poller:
    """Serial refresh with backoff; it has no reviewer capability."""

    def __init__(self, refresh: Callable[[], Awaitable[None]], interval: float) -> None:
        self.refresh = refresh
        self.interval = interval
        self.paused = False

    async def run(self) -> None:
        failures = 0
        while True:
            if not self.paused:
                try:
                    await self.refresh()
                    failures = 0
                except Exception:
                    failures = min(failures + 1, 5)
            await asyncio.sleep(self.interval * 2**failures)


async def poll_configured(path: Path | None) -> None:
    reports = await sync_configured(path)
    if any(not report.complete for report in reports):
        raise ForgeError(
            "Refresh pending: " + "; ".join(w for report in reports for w in report.warnings)
        )


async def preferences_configured(path: Path | None) -> dict[str, str]:
    async with local_store(path) as (queue, _):
        return await SQLiteDashboardStore(queue.database).preferences()


async def save_sort_configured(path: Path | None, sort: SortSpec) -> None:
    async with local_store(path) as (queue, _):
        store = SQLiteDashboardStore(queue.database)
        await store.set_preference("sort_by", sort.column)
        await store.set_preference("sort_desc", str(sort.descending).lower())


async def cancel_task_configured(path: Path | None, task_id: UUID) -> None:
    async with local_store(path) as (queue, _):
        store = SQLiteReviewStore(queue.database)
        run = await store.latest_run(task_id)
        if run is not None:
            await store.cancel_review(run.id)


async def diff_configured(path: Path | None, task_id: UUID) -> str:
    """Explicit source inspection of the saved run's pinned pair; no project execution."""
    from diffbutler.reviewers.redaction import redact_and_report
    from diffbutler.sources.git import GitSourceReader

    async with local_store(path) as (queue, settings):
        detail = await DashboardService(
            SQLiteDashboardStore(queue.database), SQLiteReviewStore(queue.database)
        ).detail(task_id)
        if not detail.cycle.runs:
            raise ReviewConflict("No reviewed revision is available")
        pr = detail.cycle.pull_request
        repo = settings.repos.get(f"{pr.ref.namespace}/{pr.ref.repository}")
        run = detail.cycle.runs[-1]
        from diffbutler.core.native import session_directory
        from diffbutler.reviewers.resources import OwnedDirectories

        store = SQLiteReviewStore(queue.database)
        resources = await store.session_resources(run.session_id)
        managed_path: Path | None = None
        if resources:
            preset = next((r.preset for r in settings.reviewers if r.id == run.reviewer_id), None)
            root = await session_directory(
                store,
                await OwnedDirectories.reopen(settings.general.workspace_root),
                run.session_id,
                preset or "codex",
            )
            candidate = root / f"source-{run.id}.git"
            if candidate.is_dir():
                managed_path = candidate
        repository_path = managed_path or (repo.path if repo else None)
        if repository_path is None:
            raise ReviewConflict("Pinned source is unavailable or has been removed by cleanup")
        source = await GitSourceReader.open(
            repository_path, base_sha=run.base_sha, head_sha=run.head_sha
        )
        diff = await source.read_diff()
        return redact_and_report(diff.encode()).decode()


async def log_configured(path: Path | None, task_id: UUID) -> str:
    async with local_store(path) as (queue, _):
        store = SQLiteReviewStore(queue.database)
        run = await store.latest_run(task_id)
        result = await store.get_result(run.id) if run else None
        if result is None or result.stdout is None:
            return "Raw output is unavailable or has been removed by cleanup."
        return (
            result.stdout.decode(errors="replace")
            + "\n"
            + (result.stderr or b"").decode(errors="replace")
        )


async def pause_configured(path: Path | None, paused: bool) -> None:
    async with local_store(path) as (queue, _):
        await SQLiteDashboardStore(queue.database).set_preference(
            "reviews_paused", str(paused).lower()
        )


async def toggle_monitoring_configured(path: Path | None) -> bool:
    """Persist discovery scope without starting reviews or contacting a forge."""
    async with local_store(path) as (queue, settings):
        store = SQLiteDashboardStore(queue.database)
        enabled = (await store.preferences()).get("monitor_all_prs") != "true"
        if enabled and (
            not settings.forges or any(not forge.repositories for forge in settings.forges)
        ):
            raise ForgeError("Set repositories for each forge before monitoring all open PRs")
        await store.set_preference("monitor_all_prs", str(enabled).lower())
        return enabled
