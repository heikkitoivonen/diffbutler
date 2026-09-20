"""Launch the user's editor with direct argv execution and terminal access."""

import asyncio
import os
import shlex
from contextlib import suppress
from pathlib import Path

from diffbutler.config.settings import ConfigError


async def edit_file(path: Path) -> None:
    try:
        command = shlex.split(os.environ.get("EDITOR", ""))
    except ValueError:
        raise ConfigError("EDITOR contains invalid quoting") from None
    if not command:
        raise ConfigError("Set EDITOR to the editor command to use with --edit")

    try:
        # Inherit the terminal and environment; the path is always one argument.
        process = await asyncio.create_subprocess_exec(*command, str(path))
    except OSError, ValueError:
        raise ConfigError("Could not launch EDITOR; check the executable and arguments") from None
    try:
        code = await process.wait()
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    if code:
        raise ConfigError(f"Editor exited with status {code}")
