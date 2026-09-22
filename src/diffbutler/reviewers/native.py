"""Native agent launch contract. Sandboxing belongs to the installed agent CLI."""

import asyncio
import json
import logging
import os
import signal
import tomllib
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import cast
from uuid import UUID

from diffbutler.reviewers.base import ReviewOutput
from diffbutler.reviewers.extraction import StreamFormat

CODEX_OUTPUT = StreamFormat(
    assistant_type="item.completed",
    text_path=("item", "text"),
    session_type="thread.started",
    session_path=("thread_id",),
    complete_type="turn.completed",
    failure_types=("error", "turn.failed"),
    assistant_filter_path=("item", "type"),
    assistant_filter_value="agent_message",
    require_session=True,
)


CLAUDE_OUTPUT = StreamFormat(
    assistant_type="assistant",  # Only the final result is an answer.
    session_type="system",
    session_path=("session_id",),
    session_filter_path=("subtype",),
    session_filter_value="init",
    complete_type="result",
    terminal_text_path=("result",),
    terminal_success_path=("subtype",),
    terminal_success_value="success",
    terminal_error_path=("is_error",),
    require_session=True,
)


class NativeError(RuntimeError):
    """Fixed local diagnostic without captured agent text or credentials."""


@dataclass(frozen=True)
class NativePreset:
    background: tuple[str, ...]
    background_resume: tuple[str, ...]
    foreground: tuple[str, ...]
    name: str = "codex"

    @property
    def output_format(self) -> StreamFormat:
        return CLAUDE_OUTPUT if self.name == "claude" else CODEX_OUTPUT

    def command_at(self, command: tuple[str, ...], cwd: Path) -> tuple[str, ...]:
        return (*command, "-C", str(cwd)) if self.name == "codex" else command

    def argv(
        self, command: tuple[str, ...], session_id: str | None, *, foreground: bool = False
    ) -> tuple[str, ...]:
        if session_id is not None:
            try:
                UUID(session_id)
            except ValueError:
                raise NativeError("Invalid native conversation identity") from None
        if foreground and session_id is None:
            raise NativeError("No saved native conversation to resume")
        arguments = (
            self.foreground
            if foreground
            else (self.background_resume if session_id else self.background)
        )
        # Opaque identities occupy a single argv element, never a shell/template.
        return (
            *command,
            *((session_id or "") if arg == "{session_id}" else arg for arg in arguments),
        )


def codex_preset() -> NativePreset:
    return native_preset("codex")


def native_preset(name: str) -> NativePreset:
    if name not in {"codex", "claude"}:
        raise NativeError("Unsupported native reviewer preset")
    data = tomllib.loads(files("diffbutler.reviewers").joinpath("presets.toml").read_text())[name]
    return NativePreset(
        tuple(data["background"]),
        tuple(data["background_resume"]),
        tuple(data["foreground"]),
        name,
    )


class CapturedProcess:
    """Own a trusted CLI's process group and bounded pipes, including spawn races.

    Deliberately detached processes are outside this native CLI supervision model.
    EOF never releases ownership: stop_and_wait must first stop and reap the group.
    """

    def __init__(self) -> None:
        self.spawn: asyncio.Task[asyncio.subprocess.Process] | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.readers: list[asyncio.Task[bytes]] = []

    async def capture(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input: bytes,
        maximum: int,
        checkpoint: Callable[[str], Awaitable[None]] | None = None,
        output_format: StreamFormat = CODEX_OUTPUT,
    ) -> ReviewOutput:
        if self.spawn is not None:
            raise NativeError("A process execution cannot be reused")
        logging.getLogger(__name__).info("Background agent starting")
        self.spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        )
        self.process = await asyncio.shield(self.spawn)
        process = self.process
        assert process is not None
        assert (
            process.stdin is not None and process.stdout is not None and process.stderr is not None
        )
        size = 0

        async def collect(stream: asyncio.StreamReader, *, events: bool) -> bytes:
            nonlocal size
            result = bytearray()
            pending = bytearray()
            while chunk := await stream.read(65536):
                size += len(chunk)
                if size > maximum:
                    raise NativeError("Reviewer output exceeds byte budget")
                result.extend(chunk)
                if events and checkpoint is not None:
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending = bytearray(rest)
                        try:
                            event = cast(object, json.loads(line))
                        except ValueError:
                            continue  # Strict final extraction rejects malformed output.
                        if isinstance(event, dict):
                            fields = cast(dict[str, object], event)
                            if fields.get("type") == output_format.session_type and (
                                output_format.session_filter_path is None
                                or fields.get(output_format.session_filter_path[0])
                                == output_format.session_filter_value
                            ):
                                identity = fields.get(output_format.session_path[0])
                                if not isinstance(identity, str):
                                    raise NativeError("Invalid native conversation identity")
                                await checkpoint(identity)
            return bytes(result)

        self.readers = [
            asyncio.create_task(collect(process.stdout, events=True)),
            asyncio.create_task(collect(process.stderr, events=False)),
        ]
        try:
            process.stdin.write(input)
            await process.stdin.drain()
        except BrokenPipeError, ConnectionResetError:
            pass
        finally:
            process.stdin.close()
        stdout, stderr = await asyncio.gather(*self.readers)
        code = await process.wait()
        logging.getLogger(__name__).info("Background agent exited code=%d", code)
        return ReviewOutput(stdout, stderr, code)

    async def stop_and_wait(self) -> None:
        if self.spawn is None:
            return
        try:
            self.process = await asyncio.shield(self.spawn)
        except OSError:
            return  # Spawn failed; no process exists.
        process = self.process
        assert process is not None
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        for reader in self.readers:
            reader.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)

        async def drain(stream: asyncio.StreamReader | None) -> None:
            if stream is not None:
                while await stream.read(65536):
                    pass

        await asyncio.gather(drain(process.stdout), drain(process.stderr))
        await process.wait()
