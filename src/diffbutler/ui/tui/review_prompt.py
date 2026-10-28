"""Explicit PR lookup and repository selection before requesting a review."""

import asyncio
import logging
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static

from diffbutler.config.settings import ConfigError
from diffbutler.core.application import (
    follow_review_target_configured,
    review_targets_configured,
)
from diffbutler.core.pr_lookup import ReviewTarget
from diffbutler.core.sync import PullRequestRow
from diffbutler.forges.base import ForgeError

LOOKUP_TIMEOUT = 60


class ReviewPrompt(ModalScreen[PullRequestRow | None]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss(None)", "Cancel")]
    CSS = """
    ReviewPrompt { align: center middle; }
    ReviewPrompt > Vertical { width: 90%; height: auto; max-height: 90%; padding: 1 2; }
    ReviewPrompt DataTable { height: 10; display: none; }
    """

    def __init__(self, path: Path | None) -> None:
        super().__init__()
        self.path = path
        self.targets: tuple[ReviewTarget, ...] = ()
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Which PR should be reviewed? Enter a link or PR number.")
            yield Input(placeholder="https://github.com/owner/repo/pull/123 or 123")
            yield Static("Enter to request a review · Escape to cancel", id="lookup-status")
            yield DataTable[Text](cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable[Text]).add_columns("Repository", "PR", "Title", "Account")
        self.query_one(Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.targets = ()
        self.query_one(DataTable[Text]).display = False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.start_lookup(event.value)

    def start_lookup(self, value: str | ReviewTarget) -> None:
        if not self.busy:
            self.busy = True
            self.query_one(Input).disabled = True
            self.query_one("#lookup-status", Static).update("Looking up open PRs…")
            self.run_worker(self.lookup(value), exclusive=True, exit_on_error=False)

    async def lookup(self, value: str | ReviewTarget) -> None:
        try:
            async with asyncio.timeout(LOOKUP_TIMEOUT):
                self.targets = (
                    (value,)
                    if isinstance(value, ReviewTarget)
                    else await review_targets_configured(self.path, value)
                )
                if len(self.targets) == 1:
                    row = await follow_review_target_configured(self.path, self.targets[0])
                    self.dismiss(row)
                    return
            table = self.query_one(DataTable[Text])
            table.clear()
            for index, target in enumerate(self.targets):
                pr = target.pull_request
                values = (
                    f"{pr.ref.namespace}/{pr.ref.repository}",
                    str(pr.ref.number),
                    pr.title,
                    f"{target.account.login} · {target.account.forge}",
                )
                table.add_row(
                    *(Text("".join(c if c.isprintable() else " " for c in v)) for v in values),
                    key=str(index),
                )
            table.display = bool(self.targets)
            self.query_one("#lookup-status", Static).update(
                "Multiple matches: select a repository and press Enter to review."
                if self.targets
                else "No accessible open PR found. Check the link or number and try again."
            )
            if self.targets:
                table.focus()
        except TimeoutError:
            self.query_one("#lookup-status", Static).update(
                "PR lookup timed out. Retry, or enter the full PR link to narrow the lookup."
            )
        except (ForgeError, ConfigError) as exc:
            self.query_one("#lookup-status", Static).update(Text(str(exc)))
        except Exception:
            logging.getLogger(__name__).exception("PR lookup failed")
            self.query_one("#lookup-status", Static).update("PR lookup failed; retry.")
        finally:
            self.busy = False
            self.query_one(Input).disabled = False
            if len(self.targets) <= 1:
                self.query_one(DataTable[Text]).display = False
                self.query_one(Input).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if not self.busy and self.targets:
            self.start_lookup(self.targets[int(str(event.row_key.value))])
