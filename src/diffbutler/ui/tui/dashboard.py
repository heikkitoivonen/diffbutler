"""Textual dashboard over the same configured core used by the CLI."""

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from uuid import UUID

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import VerticalScroll
from textual.coordinate import Coordinate
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from diffbutler.config.settings import Settings
from diffbutler.core.dashboard import (
    CycleDetail,
    DashboardRow,
    HistoryFilter,
    Poller,
    SortSpec,
    cancel_task_configured,
    column_value,
    dashboard_rows,
    detail_configured,
    diff_configured,
    history_configured,
    log_configured,
    pause_configured,
    poll_configured,
    preferences_configured,
    save_sort_configured,
    sort_rows,
    state_configured,
    toggle_monitoring_configured,
)
from diffbutler.core.managed import (
    approved_configured,
    cleanup_configured,
    handoff_configured,
    review_configured,
)
from diffbutler.core.native import foreground_environment
from diffbutler.domain.dashboard import CycleRecord
from diffbutler.forges.base import ForgeError
from diffbutler.ui.tui.app import TerminalApp
from diffbutler.ui.tui.browse import BrowseScreen
from diffbutler.ui.tui.browser import open_browser
from diffbutler.ui.tui.foreground import run_foreground


def safe(value: object) -> str:
    return "".join(char if char.isprintable() or char == "\n" else " " for char in str(value))


def queue_widths(
    ids: list[str], labels: list[str], data: list[tuple[str, tuple[str, ...]]], width: int
) -> list[int]:
    """Fit columns in terminal cells, reserving the remaining space for the title."""
    widths = [
        max(cell_len(label), *(cell_len(cells[index]) for _, cells in data))
        if data
        else cell_len(label)
        for index, label in enumerate(labels)
    ]
    if "author" in ids:
        index = ids.index("author")
        widths[index] = min(16, widths[index])
    available = max(80, width) - 2 * len(ids)
    if "title" in ids:
        index = ids.index("title")
        widths[index] = max(1, available - sum(widths) + widths[index])
    while sum(widths) > available:
        index = widths.index(max(widths))
        widths[index] -= 1
    return widths


def queue_cell(value: str, column: str, width: int) -> Text:
    value = safe(value).replace("\n", " ")
    if column == "author" and len(value) > 15:
        value = value[:15] + "…"
    text = Text(value, no_wrap=True, overflow="ellipsis")
    text.truncate(width, overflow="ellipsis")
    return text


def detail_metadata(detail: CycleDetail) -> list[str]:
    cycle = detail.cycle
    run = cycle.runs[-1] if cycle.runs else None
    result = next((result for result in detail.results if run and result.run_id == run.id), None)
    findings = str(len(result.document.findings)) if result and result.document else "—"
    age_hours = (datetime.now(UTC) - cycle.pull_request.created_at).total_seconds() / 3600
    return [
        f"AI Status: {run.status if run else 'Not started'}",
        f"Findings: {findings}",
        f"Age: {age_hours:.0f}h",
    ]


def describe(detail: CycleDetail) -> str:
    cycle = detail.cycle
    pr = cycle.pull_request
    lines = [
        f"{pr.ref.namespace}/{pr.ref.repository}#{pr.ref.number} · cycle {cycle.cycle}",
        pr.title,
        pr.url,
        *detail_metadata(detail),
        f"Current/saved head: {pr.head_sha}",
        f"Completion: {cycle.completion_reason or 'unfinished'} · {cycle.completed_at or ''}",
        f"Cleanup: {cycle.cleanup_status} · {cycle.cleanup_error or ''}",
    ]
    for session in cycle.sessions:
        lines.append(f"Session: {session.display_name} · {session.status}")
    for run in cycle.runs:
        lines.append(f"\nReviewed {run.head_sha} · {run.reviewer_id} · {run.status}")
        result = next((result for result in detail.results if result.run_id == run.id), None)
        if result is None:
            continue
        if result.error:
            lines.append(result.error)
        if result.document:
            orientation = result.document.orientation
            lines.extend(["\nIntent: " + orientation.intent, "Shape: " + orientation.shape])
            for location in orientation.look_here:
                lines.append(f"Look here: {location.path}:{location.line} · {location.why}")
            lines.extend("Low risk: " + item for item in orientation.low_risk)
            lines.extend("Unverified: " + item for item in orientation.unverified)
            if not result.document.findings:
                lines.append("No issues found — ready for your human review.")
            for finding in result.document.findings:
                lines.extend(
                    [
                        f"\n{finding.severity} · {finding.path}:{finding.line}",
                        finding.title,
                        finding.body,
                    ]
                )
    if cycle.cleanup_status == "succeeded":
        lines.append(
            "\nTemporary sources, native session and raw output deleted; analysis retained."
        )
    for receipt in cycle.receipts:
        lines.append(
            f"\nForge {receipt.id} · {receipt.author} · {receipt.created_at}\n{receipt.body}"
        )
    return safe("\n".join(lines))


def preview_summary(detail: CycleDetail) -> str:
    cycle = detail.cycle
    pr = cycle.pull_request
    lines = [
        f"#{pr.ref.number} {pr.title}",
        f"Repository: {pr.ref.namespace}/{pr.ref.repository}",
        *detail_metadata(detail),
    ]
    result = detail.results[-1] if detail.results else None
    if result and result.document:
        doc = result.document
        lines.append(
            "No issues found — your turn."
            if not doc.findings
            else f"{len(doc.findings)} findings · v to discuss"
        )
        lines.append("Intent: " + doc.orientation.intent)
        lines.extend(
            f"Look here: {item.path}:{item.line} · {item.why}" for item in doc.orientation.look_here
        )
        lines.extend("Unverified: " + item for item in doc.orientation.unverified)
        lines.extend(
            f"{item.severity}: {item.title} · {item.path}:{item.line}" for item in doc.findings
        )
    elif result and result.error:
        lines.append(result.error)
    if cycle.runs:
        run = cycle.runs[-1]
        lines.append(f"Analyzed {run.head_sha[:12]} · current {pr.head_sha[:12]} · {run.status}")
    if cycle.sessions:
        lines.append("Session: " + cycle.sessions[-1].display_name)
    if cycle.opened_at:
        lines.append("Opened for manual review · " + cycle.opened_at.isoformat())
    lines.append("Enter for full orientation, findings, receipts and cleanup status.")
    return safe("\n".join(lines))


class DetailScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss", "Back")]
    CSS = "DetailScreen { padding: 2 4; } VerticalScroll { background: $surface; padding: 1 2; }"

    def __init__(self, content: str) -> None:
        super().__init__()
        self.content = content

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(Text(self.content))
        yield Footer()


class Dashboard(TerminalApp):
    TITLE = "DiffButler"
    BINDINGS: ClassVar[list[BindingType]] = [
        ("o", "open_in_browser", "Open PR"),
        ("w", "mark_waiting_for_author", "Wait for author"),
        ("r", "run_review", "Review"),
        ("v", "resume_session", "Discuss"),
        ("m", "mark_approved", "Mark approved"),
        ("h", "view_history", "History"),
        ("b", "browse_prs", "Browse all PRs"),
        ("a", "toggle_monitoring", "Monitor all PRs"),
        ("R", "refresh", "Refresh"),
        ("space", "primary_action", "Next action"),
        ("enter", "open_detail", "Detail"),
        ("D", "view_diff", "Pinned diff"),
        ("L", "view_run_log", "Run log"),
        ("C", "cleanup_session", "Retry cleanup"),
        ("s", "sort", "Sort"),
        ("S", "reverse_sort", "Reverse"),
        ("f", "cycle_filter_preset", "View"),
        ("/", "filter", "Filter"),
        ("ctrl+c", "cancel_review", "Cancel review"),
        ("P", "toggle_pause", "Pause AI dispatch"),
        ("y", "copy_url", "Copy URL"),
        ("?", "help", "Help"),
        ("q", "quit", "Quit"),
    ]
    CSS = """
    #queue { height: 45%; }
    #filter { height: 3; }
    #status { height: 2; }
    #preview-scroll { height: 1fr; }
    #preview { padding: 1 2; }
    #primary { dock: bottom; width: 100%; }
    """

    def __init__(self, path: Path | None, settings: Settings, *, offline: bool = False) -> None:
        super().__init__()
        self.path = path
        self.settings = settings
        self.offline = offline
        self.rows: dict[str, DashboardRow] = {}
        self.history: dict[str, CycleRecord] = {}
        self.view = settings.ui.default_filter
        if self.view not in {"active", "requested", "following", "triage", "posted", "history"}:
            self.view = "active"
        self.sort = SortSpec(column=settings.ui.sort_by, descending=settings.ui.sort_desc)
        self.attached = False
        self.ai_paused = False
        self.monitor_all_prs = False
        self.reload_lock = asyncio.Lock()
        self.sync_lock = asyncio.Lock()
        self.review_tasks: dict[str, asyncio.Task[None]] = {}
        self.poller = Poller(self.sync, settings.general.poll_interval_s)
        self.ready = False
        self.closing = False
        self.preview_content = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Local queue", id="status", markup=False)
        yield Input(placeholder="Filter by repository, number, title or author", id="filter")
        yield DataTable[Text](cursor_type="row", id="queue")
        with VerticalScroll(id="preview-scroll"):
            yield Static("Select a PR", id="preview", markup=False)
        yield Button("Select a PR", id="primary")
        yield Footer()

    async def on_mount(self) -> None:
        for index in range(1, min(9, len(self.settings.ui.columns)) + 1):
            self._bindings.bind(str(index), f"sort_column({index - 1})", "Sort column", show=False)
        for action, keys in self.settings.keys.items():
            if any(action == binding[1] for binding in self.BINDINGS if isinstance(binding, tuple)):
                for key, bindings in list(self._bindings.key_to_bindings.items()):
                    self._bindings.key_to_bindings[key] = [
                        binding for binding in bindings if binding.action != action
                    ]
                self._bindings.bind(",".join(keys), action, action.replace("_", " "))
        preferences = await preferences_configured(self.path)
        self.ai_paused = preferences.get("reviews_paused") == "true"
        self.monitor_all_prs = preferences.get("monitor_all_prs") == "true"
        if not self.monitor_all_prs and self.view == "active":
            self.view = "requested"
        if (
            preferences.get("sort_by") in self.settings.ui.columns
            or preferences.get("sort_by") == "attention"
        ):
            self.sort = SortSpec(
                column=preferences["sort_by"], descending=preferences.get("sort_desc") == "true"
            )
        self.query_one(DataTable[Text]).focus()
        self.ready = True
        await self.reload()
        self.set_interval(1, self.local_tick)
        if not self.offline:
            self.run_worker(self.poller.run(), name="forge polling", exit_on_error=False)
        if self.settings.ui.default_filter == "all":
            self.action_browse_prs()

    def selected_key(self) -> str | None:
        if len(self.screen_stack) > 1:
            return None
        table = self.query_one(DataTable[Text])
        if not table.row_count:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def selected(self) -> DashboardRow | None:
        return self.rows.get(self.selected_key() or "")

    def status(self, message: str) -> None:
        if not self.attached and len(self.screen_stack) == 1:
            self.query_one("#status", Static).update(safe(message))

    async def local_tick(self) -> None:
        if self.ready and not self.closing and not self.attached and not self.reload_lock.locked():
            await self.reload()

    def on_resize(self, _: Resize) -> None:
        if self.ready:
            self.call_after_refresh(self.reload)

    async def reload(self) -> None:
        if self.attached or self.closing or len(self.screen_stack) > 1:
            return
        async with self.reload_lock:
            text = self.query_one(Input).value
            if self.view == "history":
                cycles = await history_configured(self.path, HistoryFilter())
                self.history = {
                    str(cycle.task_id): cycle
                    for cycle in cycles
                    if text.lower()
                    in (
                        f"{cycle.pull_request.title} {cycle.pull_request.ref.namespace}/"
                        f"{cycle.pull_request.ref.repository}#{cycle.pull_request.ref.number} "
                        f"{cycle.completion_reason}"
                    ).lower()
                }
                data = [
                    (
                        key,
                        (
                            cycle.pull_request.title,
                            f"Cycle {cycle.cycle}",
                            cycle.completion_reason or "",
                            cycle.cleanup_status,
                        ),
                    )
                    for key, cycle in self.history.items()
                ]
                columns = ["PR", "Cycle", "Completion", "Cleanup"]
                column_ids = ["title", "cycle", "completion", "cleanup"]
            else:
                rows = sort_rows(await dashboard_rows(self.path, self.view, text), self.sort)
                self.rows = {str(row.row.tracked.task_id): row for row in rows}
                data = [
                    (key, tuple(self.cell(row, column) for column in self.settings.ui.columns))
                    for key, row in self.rows.items()
                ]
                columns = [
                    " ".join(
                        {"number": "PR", "ai": "AI", "ci": "CI", "url": "URL"}.get(
                            word, word.title()
                        )
                        for word in column.split("_")
                    )
                    + (
                        (" ▼" if self.sort.descending else " ▲")
                        if column == self.sort.column
                        else ""
                    )
                    for column in self.settings.ui.columns
                ]
                column_ids = self.settings.ui.columns
            if self.attached:
                return
            table = self.query_one(DataTable[Text])
            wide = self.size.width >= 80
            table.styles.overflow_x = "hidden" if wide else "auto"
            table.styles.overflow_y = "hidden" if wide else "auto"
            widths = queue_widths(column_ids, columns, data, self.size.width)
            selected = self.selected_key()
            old_cursor = table.cursor_row
            # Update cells in place while identity/order is unchanged.
            keys = [key for key, _ in data]
            existing = [
                str(table.coordinate_to_cell_key(Coordinate(index, 0)).row_key.value)
                for index in range(table.row_count)
            ]
            if (
                keys != existing
                or [str(column.label) for column in table.columns.values()] != columns
                or [column.width for column in table.columns.values()] != widths
            ):
                table.clear(columns=True)
                for label, width in zip(columns, widths, strict=True):
                    table.add_column(label, width=width)
                for key, cells in data:
                    table.add_row(
                        *(
                            queue_cell(value, column, width)
                            for value, column, width in zip(cells, column_ids, widths, strict=True)
                        ),
                        key=key,
                    )
                table.move_cursor(
                    row=keys.index(selected)
                    if selected in keys
                    else min(old_cursor, max(0, len(keys) - 1))
                )
            else:
                for index, (_, cells) in enumerate(data):
                    for col, value in enumerate(cells):
                        content = queue_cell(value, column_ids[col], widths[col])
                        if table.get_cell_at(Coordinate(index, col)) != content:
                            table.update_cell_at(Coordinate(index, col), content)
            mode = "offline" if self.offline else "paused" if self.poller.paused else "live"
            scope = "all open PRs" if self.monitor_all_prs else "review requests"
            self.sub_title = f"{self.view} · {len(keys)} PRs · {mode} · Monitoring: {scope}" + (
                " · AI paused" if self.ai_paused else ""
            )
            await self.preview()

    def cell(self, row: DashboardRow, column: str) -> str:
        value = column_value(row, column)
        if column == "age" and isinstance(value, (int, float)):
            return f"{value / 3600:.0f}h"
        if column == "max_severity":
            return row.max_severity or "—"
        if column == "state" and row.row.tracked.refresh_pending:
            return safe(value) + " -"
        return safe(value) if value is not None else "—"

    async def preview(self) -> None:
        key = self.selected_key()
        if key is None:
            self.preview_content = ""
            self.query_one("#preview", Static).update("No PRs in this view.")
            self.query_one(Button).label = "Select a PR"
            return
        row = self.selected() if self.view != "history" else None
        label = (
            "Open historical PR"
            if self.view == "history"
            else {
                "PENDING_AI_REVIEW": "Start AI review",
                "FINDINGS_TRIAGE": "Discuss findings",
                "QUEUED": "Review queued · Ctrl+C to cancel",
                "AI_RUNNING": "Review running · Ctrl+C to cancel",
            }.get(row.row.tracked.state if row else "", "Open PR for human review")
        )
        self.query_one(Button).label = label
        if not self.settings.ui.preview_pane:
            return
        detail = await detail_configured(self.path, UUID(key))
        if self.selected_key() == key and not self.attached:
            notice = row.row.tracked.notice + "\n" if row else ""
            content = notice + (
                describe(detail) if self.view == "history" else preview_summary(detail)
            )
            if content != self.preview_content:
                self.query_one("#preview", Static).update(Text(content))
                self.preview_content = content

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if len(self.screen_stack) == 1:
            event.stop()
            await self.action_open_detail()

    async def on_data_table_row_highlighted(self, _: DataTable.RowHighlighted) -> None:
        if not self.reload_lock.locked() and not self.attached and len(self.screen_stack) == 1:
            await self.preview()

    async def on_input_changed(self, _: Input.Changed) -> None:
        if self.ready and len(self.screen_stack) == 1:
            await self.reload()

    def on_input_submitted(self, _: Input.Submitted) -> None:
        self.query_one(DataTable[Text]).focus()

    def action_filter(self) -> None:
        self.query_one(Input).focus()

    async def on_button_pressed(self, _: Button.Pressed) -> None:
        self.action_primary_action()

    def start(self, operation: Awaitable[None]) -> None:
        self.run_worker(self.guarded(operation), exit_on_error=False)

    async def guarded(self, operation: Awaitable[None]) -> None:
        try:
            await operation
        except ForgeError as exc:
            self.status(f"Operation failed: {exc}")
        except Exception as exc:
            self.status(
                f"Operation failed ({type(exc).__name__}); retry or use the CLI for diagnostics."
            )
        finally:
            if not self.attached:
                await self.reload()

    async def sync(self) -> None:
        if self.sync_lock.locked():
            return
        async with self.sync_lock:
            try:
                await poll_configured(self.path)
                self.status("Forge refreshed")
            except ForgeError as exc:
                self.status(f"Refresh pending: {exc}. Previous evidence retained.")
                raise
            except Exception:
                self.status("Refresh pending; previous evidence retained. Polling will retry.")
                raise
            finally:
                if not self.attached:
                    await self.reload()

    def action_refresh(self) -> None:
        self.start(self.sync())

    async def open_selected(self) -> None:
        key = self.selected_key()
        if self.view == "history":
            cycle = self.history.get(key or "")
            if cycle is None:
                return
            url = cycle.pull_request.url
        else:
            row = self.selected()
            if row is None:
                return
            url = await state_configured(
                self.path, row.row.tracked.pull_request.url, row.row.account.login, waiting=False
            )
        try:
            opened = await open_browser(url, self.settings.ui.browser_command)
        except OSError, TimeoutError:
            opened = False
        if not opened:
            self.copy_to_clipboard(url)
            self.status(f"Open this URL (copied if supported): {url}")
        if self.view != "history":
            table = self.query_one(DataTable[Text])
            table.move_cursor(row=min(table.cursor_row + 1, table.row_count - 1))

    def action_open_in_browser(self) -> None:
        self.start(self.open_selected())

    def action_copy_url(self) -> None:
        key = self.selected_key()
        cycle = self.history.get(key or "") if self.view == "history" else None
        row = self.selected()
        url = cycle.pull_request.url if cycle else row.row.tracked.pull_request.url if row else ""
        if url:
            self.copy_to_clipboard(url)
            self.status(url)

    async def state_action(self, *, approve: bool = False) -> None:
        row = self.selected()
        if row is None or self.view == "history":
            return
        pr = row.row.tracked.pull_request
        if approve:
            status = await approved_configured(self.path, pr.url, row.row.account.login)
            self.status(f"Completion recorded locally · cleanup: {status}")
        else:
            await state_configured(self.path, pr.url, row.row.account.login, waiting=True)

    def action_mark_waiting_for_author(self) -> None:
        self.start(self.state_action())

    def action_mark_approved(self) -> None:
        self.start(self.state_action(approve=True))

    def action_run_review(self) -> None:
        row = self.selected()
        key = self.selected_key()
        if row is None or key is None or self.view == "history" or key in self.review_tasks:
            return

        async def review() -> None:
            try:
                result = await review_configured(
                    self.path, row.row.tracked.pull_request.url, row.row.account.login, None
                )
                self.status(f"AI review {result.status}; no publication or approval implied.")
            finally:
                self.review_tasks.pop(key, None)

        self.review_tasks[key] = asyncio.create_task(self.guarded(review()))

    async def action_cancel_review(self) -> None:
        key = self.selected_key()
        if key is None or self.view == "history":
            return
        await cancel_task_configured(self.path, UUID(key))
        task = self.review_tasks.get(key)
        if task:
            task.cancel()
        await self.reload()

    async def attach(self, row: DashboardRow) -> None:
        if self.attached:
            return
        self.attached = True
        error: BaseException | None = None
        code: int | None = None
        try:
            async with handoff_configured(
                self.path, row.row.tracked.pull_request.url, row.row.account.login
            ) as (launch, started):
                with self.suspend():
                    try:
                        code = await run_foreground(
                            launch.argv,
                            cwd=launch.cwd,
                            env=foreground_environment(launch),
                            started=started,
                        )
                    except BaseException as exc:
                        error = exc
                # Textual's suspend must exit normally to restore the dashboard.
                if error is not None:
                    raise error
        finally:
            self.attached = False
        self.status(f"Agent exited {code}; forge refresh attempted. Check refresh-pending badges.")

    def action_resume_session(self) -> None:
        row = self.selected()
        if row and self.view != "history" and not self.attached:
            self.start(self.attach(row))

    def action_primary_action(self) -> None:
        row = self.selected()
        if self.view == "history":
            self.action_open_in_browser()
        elif row:
            state = row.row.tracked.state
            if state == "PENDING_AI_REVIEW":
                self.action_run_review()
            elif state == "FINDINGS_TRIAGE":
                self.action_resume_session()
            elif state in {"QUEUED", "AI_RUNNING"}:
                self.status("Review in progress; Ctrl+C cancels the selected local run.")
            else:
                self.action_open_in_browser()

    async def action_open_detail(self) -> None:
        key = self.selected_key()
        if key:
            self.push_screen(DetailScreen(describe(await detail_configured(self.path, UUID(key)))))

    async def action_view_diff(self) -> None:
        key = self.selected_key()
        if key:

            async def show() -> None:
                content = await diff_configured(self.path, UUID(key))
                self.push_screen(DetailScreen(safe(content)))

            self.start(show())

    async def action_view_run_log(self) -> None:
        key = self.selected_key()
        if key:
            self.push_screen(DetailScreen(safe(await log_configured(self.path, UUID(key)))))

    def action_browse_prs(self) -> None:
        async def followed(task_id: str | None) -> None:
            if task_id is not None:
                self.view = "active"
                self.query_one(Input).value = ""
                await self.reload()
                keys = list(self.rows)
                if task_id in keys:
                    self.query_one(DataTable[Text]).move_cursor(row=keys.index(task_id))
                self.status("PR followed; press r to explicitly start AI review.")

        self.push_screen(BrowseScreen(self.path, len(self.settings.forges)), followed)

    def action_toggle_monitoring(self) -> None:
        async def toggle() -> None:
            try:
                enabled = await toggle_monitoring_configured(self.path)
            except ForgeError as exc:
                self.status(str(exc))
                return
            self.monitor_all_prs = enabled
            self.view = "active" if enabled else "requested"
            self.query_one(Input).value = ""
            if enabled and not self.offline:
                await self.sync()
            self.status(
                "Monitoring all open PRs in configured repositories."
                if enabled
                else "Showing review requests; followed PRs remain monitored."
            )

        self.start(toggle())

    async def action_view_history(self) -> None:
        self.view = (
            ("active" if self.monitor_all_prs else "requested")
            if self.view == "history"
            else "history"
        )
        await self.reload()

    async def action_cycle_filter_preset(self) -> None:
        views = ["active", "requested", "following", "triage", "posted", "history"]
        self.view = views[(views.index(self.view) + 1) % len(views)]
        await self.reload()

    async def action_cleanup_session(self) -> None:
        self.status(f"Cleanup: {await cleanup_configured(self.path)}")
        await self.reload()

    async def save_sort(self) -> None:
        await save_sort_configured(self.path, self.sort)
        await self.reload()

    async def action_sort(self) -> None:
        columns = self.settings.ui.columns
        index = columns.index(self.sort.column) if self.sort.column in columns else -1
        self.sort = SortSpec(
            column=columns[(index + 1) % len(columns)], descending=self.sort.descending
        )
        await self.save_sort()

    async def action_reverse_sort(self) -> None:
        self.sort = self.sort.model_copy(update={"descending": not self.sort.descending})
        await self.save_sort()

    async def action_sort_column(self, index: int) -> None:
        self.sort = SortSpec(
            column=self.settings.ui.columns[index], descending=self.sort.descending
        )
        await self.save_sort()

    async def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        if self.view != "history":
            await self.action_sort_column(event.column_index)

    async def action_toggle_pause(self) -> None:
        self.ai_paused = not self.ai_paused
        await pause_configured(self.path, self.ai_paused)
        self.status(
            "AI dispatch paused; current runs finish" if self.ai_paused else "AI dispatch resumed"
        )
        await self.reload()

    def action_help(self) -> None:
        self.push_screen(
            DetailScreen(
                "r starts an AI run only on your explicit action. v resumes its native session.\n"
                "Tell the agent explicitly which feedback to post.\n"
                "Opening the session grants no posting permission.\n"
                "o chooses manual review and opens the browser. w chooses waiting for author.\n"
                "m records your completed approval locally and schedules cleanup.\n"
                "It does not submit approval.\n"
                "a toggles monitoring all open PRs in configured repositories.\n"
                "h browses offline history. C retries due cleanup. R refreshes forge evidence.\n"
                "s cycles sort columns; S reverses; 1-9 or a column header selects a sort.\n"
                "/ filters; f cycles views. Ctrl+C cancels the selected run launched here.\n"
                "P pauses AI dispatch; forge polling continues.\n"
                "q quits and cancels reviews launched in this dashboard."
            )
        )

    async def on_unmount(self) -> None:
        self.closing = True
        for key, task in list(self.review_tasks.items()):
            await cancel_task_configured(self.path, UUID(key))
            task.cancel()
        await asyncio.gather(*self.review_tasks.values(), return_exceptions=True)

    async def action_quit(self) -> None:
        await self.on_unmount()
        self.exit()
