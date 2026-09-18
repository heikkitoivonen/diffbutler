"""Accessible PR search uses the same read/follow API as the CLI."""

from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static

from diffbutler.core.application import browse_configured, follow_configured
from diffbutler.domain.forge import PRQuery, PullRequest


class BrowseScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "dismiss(None)", "Back"),
        ("enter", "follow", "Follow selected"),
        ("n", "next_page", "Next page"),
        ("f", "forge", "Next forge"),
    ]
    CSS = "BrowseScreen { padding: 2 3; } DataTable { height: 1fr; }"

    def __init__(self, path: Path | None, forge_count: int) -> None:
        super().__init__()
        self.path = path
        self.forge_count = forge_count
        self.forge_index = 1
        self.cursor: str | None = None
        self.prs: dict[str, PullRequest] = {}
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Static(
            "Browse accessible PRs · search with Enter, then select and Enter to follow",
            markup=False,
        )
        yield Input(placeholder="Search text (blank lists accessible open PRs)")
        yield Static("", id="browse-status", markup=False)
        yield DataTable[Text](cursor_type="row")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one(DataTable[Text])
        table.add_columns("Repository", "#", "Title", "Author")
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.cursor = None
        self.run_worker(self.search(), exclusive=True, exit_on_error=False)

    async def search(self) -> None:
        if self.busy:
            return
        self.busy = True
        status = self.query_one("#browse-status", Static)
        try:
            page = await browse_configured(
                self.path, self.forge_index, PRQuery(text=self.query_one(Input).value), self.cursor
            )
            self.cursor = page.next_cursor
            self.prs = {pr.url: pr for pr in page.items}
            table = self.query_one(DataTable[Text])
            table.clear()
            for key, pr in self.prs.items():
                values = (
                    f"{pr.ref.namespace}/{pr.ref.repository}",
                    str(pr.ref.number),
                    pr.title,
                    pr.author,
                )
                table.add_row(
                    *(
                        Text("".join(c if c.isprintable() else " " for c in value))
                        for value in values
                    ),
                    key=key,
                )
            status.update(
                f"Forge {self.forge_index} · {len(page.items)} PRs · "
                + ("Next page: n" if self.cursor else "Last page")
                + (" · incomplete results; retry search" if not page.complete else "")
            )
            table.focus()
        except Exception:
            status.update("Search unavailable; check forge configuration and retry.")
        finally:
            self.busy = False

    def action_next_page(self) -> None:
        if self.cursor and not self.busy:
            self.run_worker(self.search(), exit_on_error=False)

    async def action_forge(self) -> None:
        self.forge_index = self.forge_index % max(1, self.forge_count) + 1
        self.cursor = None
        await self.search()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        await self.action_follow()

    async def action_follow(self) -> None:
        if self.busy or isinstance(self.focused, Input):
            return
        table = self.query_one(DataTable[Text])
        if not table.row_count:
            return
        key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        self.busy = True
        try:
            row = await follow_configured(self.path, self.forge_index, key)
            self.dismiss(str(row.tracked.task_id))
        except Exception:
            self.query_one("#browse-status", Static).update("Could not follow this PR; retry.")
        finally:
            self.busy = False
