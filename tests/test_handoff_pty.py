"""Exercise a real Textual driver and POSIX terminal, without forge/model calls."""

import errno
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest


class Terminal:
    def __init__(
        self,
        directory: Path,
        command: list[str] | None = None,
        failure: str | None = None,
        app_argv: list[str] | None = None,
    ) -> None:
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
        self.restoration = directory / "terminal-restored.txt"
        self.events = directory / "events.jsonl"
        self.output = bytearray()
        argv = [
            sys.executable,
            "-m",
            "diffbutler.ui.tui.handoff_probe",
            "--events",
            str(self.events),
        ]
        if failure:
            # Inject an actual runner exception through the real Textual suspend path.
            bootstrap = (
                "import asyncio\n"
                "import diffbutler.ui.tui.handoff_probe as probe\n"
                "async def fail(argv):\n"
                f"    raise {failure}\n"
                "probe.run_foreground = fail\n"
                "probe.main()\n"
            )
            argv = [sys.executable, "-c", bootstrap, "--events", str(self.events)]
        if command:
            argv.extend(["--", *command])
        if app_argv is not None:
            argv = app_argv
        # Keep the session leader alive until modes have been checked: macOS
        # revokes the slave terminal when its session leader exits.
        launcher = (
            "import os,sys,fcntl,termios,pathlib,runpy\n"
            "os.setsid()\n"
            "fcntl.ioctl(0,termios.TIOCSCTTY,0)\n"
            "modes=termios.tcgetattr(0)\n"
            "restoration=pathlib.Path(sys.argv[1])\n"
            "mode, target = sys.argv[3:5]\n"
            "sys.argv=sys.argv[4:]\n"
            "try:\n"
            "    if mode == '-m':\n"
            "        runpy.run_module(target, run_name='__main__', alter_sys=True)\n"
            "    elif mode == '-c':\n"
            "        exec(target, {'__name__': '__main__'})\n"
            "    else:\n"
            "        raise ValueError('Expected Python -m or -c')\n"
            "finally:\n"
            "    restoration.write_text(str(termios.tcgetattr(0)==modes))\n"
        )
        self.process = subprocess.Popen(
            [sys.executable, "-c", launcher, str(self.restoration), *argv],
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            env={**os.environ, "TERM": "xterm-256color"},
        )

    def drain(self) -> None:
        if select.select([self.master], [], [], 0.02)[0]:
            try:
                self.output.extend(os.read(self.master, 65536))
            except OSError as error:
                if error.errno != errno.EIO:
                    raise

    def wait(self, predicate: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self.drain()
            if predicate():
                return
            if self.process.poll() is not None:
                break
        raise AssertionError(f"Terminal condition timed out. Output: {self.output[-6000:]!r}")

    def records(self, event: str) -> list[dict[str, Any]]:
        if not self.events.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.events.read_text().splitlines(keepends=True):
            if line.endswith("\n"):
                record = json.loads(line)
                if record["event"] == event:
                    records.append(record)
        return records

    def send(self, value: bytes) -> None:
        os.write(self.master, value)

    def resize(self, width: int, height: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))

    def quit(self) -> None:
        self.send(b"q")
        self.wait(lambda: self.process.poll() is not None)
        assert self.process.returncode == 0
        assert self.restoration.read_text() == "True"

    def close(self) -> None:
        if self.process.poll() is None:
            foreground = os.tcgetpgrp(self.master)
            if foreground > 0 and foreground != self.process.pid:
                os.killpg(foreground, signal.SIGKILL)
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def terminal(tmp_path: Path) -> Iterator[Terminal]:
    terminal = Terminal(tmp_path)
    try:
        terminal.wait(lambda: bool(terminal.records("ready")))
        yield terminal
    finally:
        terminal.close()


def attach(terminal: Terminal, resume: int = 1) -> None:
    terminal.send(b"v")
    terminal.wait(lambda: f"resumes={resume}".encode() in terminal.output)
    assert os.tcgetpgrp(terminal.master) != terminal.process.pid
    before = terminal.records("handoff")[-1]["ticks"]
    terminal.wait(
        lambda: any(r["attached"] and r["ticks"] >= before + 3 for r in terminal.records("tick"))
    )


def test_repeated_handoff_preserves_selection_conversation_and_background(
    terminal: Terminal,
) -> None:
    terminal.send(b"\x1b[B")
    attach(terminal)
    terminal.send(b"remember\n")
    terminal.wait(lambda: b"remembered=synthetic source review" in terminal.output)
    terminal.send(b"exit\n")
    terminal.wait(lambda: len(terminal.records("returned")) == 1)
    first = terminal.records("returned")[0]
    assert first["row"] == 1
    assert first["tick_delta"] >= 3
    assert first["foreground"]
    assert first["background_bytes"] > 0
    assert b"BACKGROUND OUTPUT MUST NOT REACH TERMINAL" not in terminal.output
    attach(terminal, 2)
    terminal.send(b"exit\n")
    terminal.wait(lambda: len(terminal.records("returned")) == 2)
    assert terminal.records("returned")[1]["row"] == 1
    assert terminal.records("returned")[1]["background_bytes"] > first["background_bytes"]
    terminal.quit()


@pytest.mark.parametrize(
    ("command", "code"), [(b"\x03", 0), (b"crash\n", 23), (b"raw-crash\n", 24)]
)
def test_interrupt_and_crashes_restore_terminal(
    terminal: Terminal, command: bytes, code: int
) -> None:
    attach(terminal)
    terminal.send(command)
    terminal.wait(lambda: bool(terminal.records("returned")))
    result = terminal.records("returned")[0]
    assert result["code"] == code
    assert result["foreground"]
    # Reattach to prove input and output still function after damaged termios.
    attach(terminal, 2)
    terminal.send(b"exit\n")
    terminal.wait(lambda: len(terminal.records("returned")) == 2)
    terminal.quit()


def test_missing_executable_returns_to_dashboard(terminal: Terminal) -> None:
    terminal.send(b"f")
    terminal.wait(lambda: bool(terminal.records("returned")))
    assert terminal.records("returned")[0]["code"] == 127
    attach(terminal)
    terminal.send(b"exit\n")
    terminal.wait(lambda: len(terminal.records("returned")) == 2)
    terminal.quit()


@pytest.mark.parametrize(
    "failure", ["OSError('synthetic launch failure')", "asyncio.CancelledError()"]
)
def test_exception_inside_suspend_restores_dashboard(tmp_path: Path, failure: str) -> None:
    terminal = Terminal(tmp_path, failure=failure)
    try:
        terminal.wait(lambda: bool(terminal.records("ready")))
        terminal.send(b"v")
        terminal.wait(lambda: bool(terminal.records("released")))
        if failure.startswith("OSError"):
            assert "synthetic launch failure" in terminal.records("returned")[0]["error"]
        # Quitting via the dashboard proves Textual restarted its input driver.
        terminal.quit()
    finally:
        terminal.close()


def test_terminated_agent_restores_dashboard(terminal: Terminal) -> None:
    attach(terminal)
    os.killpg(os.tcgetpgrp(terminal.master), signal.SIGTERM)
    terminal.wait(lambda: bool(terminal.records("returned")))
    assert terminal.records("returned")[0]["code"] == -signal.SIGTERM
    attach(terminal, 2)
    terminal.send(b"exit\n")
    terminal.wait(lambda: len(terminal.records("returned")) == 2)
    terminal.quit()


def test_resize_during_handoff_reaches_agent_and_restored_dashboard(terminal: Terminal) -> None:
    attach(terminal)
    terminal.resize(85, 24)
    terminal.send(b"size\n")
    terminal.wait(lambda: b"AGENT_SIZE 85x24" in terminal.output)
    terminal.send(b"exit\n")
    terminal.wait(lambda: bool(terminal.records("returned")))
    terminal.wait(lambda: terminal.records("resize")[-1]["width"] == 85)
    assert terminal.records("resize")[-1]["height"] == 24
    attach(terminal, 2)
    terminal.resize(120, 35)
    terminal.send(b"size\n")
    terminal.wait(lambda: b"AGENT_SIZE 120x35" in terminal.output)
    terminal.send(b"exit\n")
    terminal.wait(lambda: len(terminal.records("returned")) == 2)
    terminal.wait(lambda: terminal.records("resize")[-1]["width"] == 120)
    terminal.quit()


def test_full_screen_child_resizes_and_returns(tmp_path: Path) -> None:
    child_events = tmp_path / "child-events.jsonl"
    terminal = Terminal(
        tmp_path,
        command=[
            sys.executable,
            "-m",
            "diffbutler.ui.tui.handoff_probe",
            "--events",
            str(child_events),
        ],
    )

    def child_records() -> list[dict[str, Any]]:
        if not child_events.exists():
            return []
        return [
            json.loads(line)
            for line in child_events.read_text().splitlines(keepends=True)
            if line.endswith("\n")
        ]

    try:
        terminal.wait(lambda: bool(terminal.records("ready")))
        terminal.send(b"v")
        terminal.wait(lambda: any(r["event"] == "ready" for r in child_records()))
        terminal.resize(92, 26)
        terminal.wait(
            lambda: any(
                r["event"] == "resize" and r["width"] == 92 and r["height"] == 26
                for r in child_records()
            )
        )
        terminal.send(b"q")
        terminal.wait(lambda: bool(terminal.records("returned")))
        assert terminal.records("returned")[0]["code"] == 0
        terminal.wait(lambda: terminal.records("resize")[-1]["width"] == 92)
        terminal.quit()
    finally:
        terminal.close()
