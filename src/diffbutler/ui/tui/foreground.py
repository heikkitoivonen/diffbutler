"""POSIX terminal ownership experiment; call only while the UI is suspended."""

import asyncio
import os
import signal
import sys
import termios
from collections.abc import Sequence
from contextlib import suppress


def _give_terminal(fd: int, process_group: int) -> None:
    # Reclaiming the terminal is a background-process operation: block SIGTTOU.
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(fd, process_group)
    finally:
        signal.signal(signal.SIGTTOU, previous)


async def run_foreground(argv: Sequence[str]) -> int:
    """Attach one process group, await without blocking asyncio, always restore TTY.

    The caller serializes handoffs. This is not a reviewer sandbox or credential
    boundary: the foreground command inherits the user's environment.
    """
    if not argv:
        raise ValueError("An agent command is required")
    fd = sys.stdin.fileno()
    if not os.isatty(fd) or os.tcgetpgrp(fd) != os.getpgrp():
        raise RuntimeError("Handoff requires ownership of a controlling terminal")
    owner = os.tcgetpgrp(fd)
    modes = termios.tcgetattr(fd)
    read_gate, write_gate = os.pipe()
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "diffbutler.ui.tui._foreground_exec",
            str(read_gate),
            *argv,
            stdin=fd,
            stdout=sys.stdout.fileno(),
            stderr=sys.stderr.fileno(),
            pass_fds=(read_gate,),
            process_group=0,
        )
        _give_terminal(fd, process.pid)
        os.write(write_gate, b"1")
        return await process.wait()
    finally:
        os.close(read_gate)
        os.close(write_gate)
        try:
            if process is not None:
                # Also remove descendants left behind by a crashed command.
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        finally:
            _give_terminal(fd, owner)
            termios.tcsetattr(fd, termios.TCSANOW, modes)
