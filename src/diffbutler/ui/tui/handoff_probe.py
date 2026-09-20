"""Runnable terminal experiment. No forge access or automatic model calls."""

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
import time
import uuid
from importlib.metadata import version
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.events import Resize
from textual.widgets import DataTable, Footer, Header, Static

from diffbutler.ui.tui.app import TerminalApp
from diffbutler.ui.tui.foreground import run_foreground


class HandoffProbe(TerminalApp):
    """Three fake PRs, a live heartbeat, and an explicitly launched agent."""

    TITLE = "DiffButler — terminal handoff experiment"
    BINDINGS: ClassVar[list[BindingType]] = [
        ("v", "handoff", "Resume agent"),
        ("f", "fail", "Bad command"),
        ("q", "quit", "Quit"),
    ]
    CSS = "DataTable { height: 8; } Static { margin: 1 2; }"

    def __init__(self, directory: Path, command: list[str], events: Path) -> None:
        super().__init__()
        self.directory = directory
        self.command = command
        self.events_path = events
        self.ticks = 0
        self.attached = False
        self.session = directory / "fake-session.json"
        self.session.write_text(
            json.dumps({"id": str(uuid.uuid4()), "resumes": 0, "seed": "synthetic source review"})
        )

    def record(self, event: str, **fields: object) -> None:
        with self.events_path.open("a") as output:
            output.write(
                json.dumps(
                    {"event": event, "time": time.monotonic(), "ticks": self.ticks, **fields}
                )
                + "\n"
            )

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable[str](cursor_type="row")
        yield Static("Select a row, then v. The default agent is an offline fake.", id="result")
        yield Static("", id="heartbeat")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable[str])
        table.add_columns("PR", "Status")
        table.add_rows([("demo#1", "Ready"), ("demo#2", "Findings"), ("demo#3", "Waiting")])
        table.focus()
        self.run_worker(self.heartbeat())
        self.run_worker(self.background_output())
        self.record(
            "ready",
            system=platform.system(),
            python=platform.python_version(),
            textual=version("textual"),
        )

    def on_resize(self, event: Resize) -> None:
        self.record("resize", width=event.size.width, height=event.size.height)

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            self.ticks += 1
            self.record("tick", attached=self.attached)
            if not self.attached:
                self.query_one("#heartbeat", Static).update(f"Background ticks: {self.ticks}")

    async def background_output(self) -> None:
        with (self.directory / "background.log").open("wb") as output:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "diffbutler.ui.tui._fake_agent",
                "--ticker",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
            try:
                await process.wait()
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()

    def action_handoff(self) -> None:
        if not self.attached:
            self.attached = True
            self.run_worker(self.handoff(self.command))

    def action_fail(self) -> None:
        if not self.attached:
            self.attached = True
            self.run_worker(self.handoff(["/nonexistent/diffbutler-agent"]))

    async def handoff(self, command: list[str]) -> None:
        table = self.query_one(DataTable[str])
        row = table.cursor_row
        before = self.ticks
        error: BaseException | None = None
        result: int | None = None
        self.record("handoff", row=row)
        try:
            with self.suspend():
                # Textual 8.2.8 resumes *after* yield, not in a finally block.
                # Defer all exceptions until outside the suspension context.
                try:
                    result = await run_foreground(command)
                except BaseException as caught:
                    error = caught
            if isinstance(error, asyncio.CancelledError):
                raise error
            if error is not None and not isinstance(error, Exception):
                raise error
            status = f"Launch failed: {error}" if error else f"Agent exited: {result}"
            self.query_one("#result", Static).update(
                f"{status}. Background advanced {self.ticks - before} ticks. Press v to resume."
            )
            self.record(
                "returned",
                code=result,
                error=str(error) if error else None,
                row=table.cursor_row,
                tick_delta=self.ticks - before,
                foreground=os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp(),
                background_bytes=(self.directory / "background.log").stat().st_size,
            )
        finally:
            self.attached = False
            self.record("released")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, help="Write diagnostic JSONL (no agent output)")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Optional foreground argv after --, e.g. codex resume SESSION_ID",
    )
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("Run in an interactive terminal; Linux and macOS are supported")
    with tempfile.TemporaryDirectory(prefix="diffbutler-handoff-") as temporary:
        directory = Path(temporary)
        command = list[str](args.command)
        if command[:1] == ["--"]:
            command = command[1:]
        if not command:
            command = [
                sys.executable,
                "-m",
                "diffbutler.ui.tui._fake_agent",
                "--session",
                str(directory / "fake-session.json"),
            ]
        events = Path(args.events) if args.events else directory / "events.jsonl"
        HandoffProbe(directory, command, events).run()


if __name__ == "__main__":
    main()
