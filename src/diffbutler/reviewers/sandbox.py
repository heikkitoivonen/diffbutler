"""Internal, offline Linux isolation primitive; not a Reviewer or dispatcher."""

import asyncio
import math
import os
import shutil
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from diffbutler.reviewers.base import ReviewOutput


class SandboxError(Exception):
    """Safe diagnostic: never include child output, input, or host paths."""


@dataclass(frozen=True)
class SandboxLimits:
    timeout_s: float = 30
    max_input_bytes: int = 1_000_000
    max_output_bytes: int = 4_000_000

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.timeout_s)
            or self.timeout_s <= 0
            or min(self.max_input_bytes, self.max_output_bytes) <= 0
        ):
            raise ValueError("sandbox limits must be positive and finite")


async def _settle[T](task: asyncio.Task[T]) -> T:
    """Finish owned cleanup despite repeated cancellation; caller re-raises it."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


class OfflineSandbox:
    """Run a trusted executable in a dedicated, credential-free runtime image.

    The caller supplies a trusted image, never a host root, repository, user home,
    or foreground session. The image and bwrap installation must remain trusted
    and unchanged during execution. No host source or session mounts are accepted.
    Network is entirely unavailable, including model providers. This primitive
    does not implement source mediation, source-only tool policy, or review auth.
    """

    async def run(
        self,
        runtime: Path,
        argv: tuple[str, ...],
        *,
        input_data: bytes = b"",
        limits: SandboxLimits | None = None,
    ) -> ReviewOutput:
        limits = limits or SandboxLimits()
        if len(input_data) > limits.max_input_bytes:
            raise SandboxError("Sandbox input exceeds the configured byte limit")
        if (
            not argv
            or not argv[0].startswith("/")
            or ".." in PurePosixPath(argv[0]).parts
            or any("\x00" in arg for arg in argv)
        ):
            raise ValueError("sandbox command requires an absolute executable and NUL-free argv")
        if sys.platform != "linux":
            raise SandboxError("Offline process isolation requires Linux and Bubblewrap")
        # Never search a repository-controlled PATH or accept extra bwrap options.
        executable = shutil.which("bwrap", path=os.defpath)
        if executable is None:
            raise SandboxError("Bubblewrap is unavailable; isolated execution was not started")
        try:
            root = await asyncio.to_thread(runtime.resolve, strict=True)
            if root == Path("/") or not root.is_dir():
                raise ValueError
            # Mount destinations must not redirect through image-provided symlinks.
            for name in ("tmp", "home", "work"):
                target = root / name
                if target.is_symlink() or not target.is_dir():
                    raise ValueError
        except OSError, ValueError:
            raise SandboxError(
                "Sandbox requires a dedicated runtime image with tmp/home/work"
            ) from None

        command = (
            executable,
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--cap-drop",
            "ALL",
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            str(root),
            "/",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/home",
            "--tmpfs",
            "/work",
            "--chdir",
            "/work",
            "--clearenv",
            "--setenv",
            "HOME",
            "/home",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "LC_ALL",
            "C",
            "--setenv",
            "PWD",
            "/work",
            "--",
            *argv,
        )
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                cwd="/",
                env={"PATH": os.defpath, "LC_ALL": "C"},
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
            )
        )
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            # Spawn can complete after the caller is cancelled. Reap it too.
            try:
                process = await _settle(launch)
            except OSError:
                raise asyncio.CancelledError from None
            await _settle(asyncio.create_task(_stop(process, ())))
            raise
        except OSError:
            raise SandboxError("Could not start isolated execution") from None

        assert process.stdin is not None
        assert process.stdout is not None and process.stderr is not None
        total = 0

        async def collect(stream: asyncio.StreamReader) -> bytes:
            nonlocal total
            result = bytearray()
            while chunk := await stream.read(65536):
                total += len(chunk)
                if total > limits.max_output_bytes:
                    raise SandboxError("Sandbox output exceeds the configured byte limit")
                result.extend(chunk)
            return bytes(result)

        async def feed() -> bytes:
            assert process.stdin is not None
            try:
                # Drain in chunks so a child which never reads cannot buffer all input.
                for start in range(0, len(input_data), 65536):
                    process.stdin.write(input_data[start : start + 65536])
                    await process.stdin.drain()
            except BrokenPipeError, ConnectionResetError:
                pass
            finally:
                process.stdin.close()
            return b""

        tasks = (
            asyncio.create_task(collect(process.stdout)),
            asyncio.create_task(collect(process.stderr)),
            asyncio.create_task(feed()),
        )
        try:
            async with asyncio.timeout(limits.timeout_s):
                stdout, stderr, _ = await asyncio.gather(*tasks)
                code = await process.wait()
                # No raw stderr in errors/logs. Nonzero includes sandbox setup errors;
                # never retry outside the sandbox or interpret this as review output.
                if code != 0:
                    raise SandboxError("Isolated execution failed")
                return ReviewOutput(stdout=stdout, stderr=stderr, exit_code=code)
        except TimeoutError:
            raise SandboxError("Isolated execution timed out") from None
        finally:
            cleanup = asyncio.create_task(_stop(process, tasks))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await _settle(cleanup)
                raise


async def _stop(
    process: asyncio.subprocess.Process, tasks: tuple[asyncio.Task[bytes], ...]
) -> None:
    # Bubblewrap's PID namespace and parent-death behavior also contain children
    # that create a new session; process-group cleanup alone does not do that.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.stdin is not None:
        process.stdin.close()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    async def discard(stream: asyncio.StreamReader | None) -> None:
        if stream is not None:
            while await stream.read(65536):
                pass

    # A full StreamReader pauses its transport. Drain after killing, including
    # cancellation during spawn before collectors exist, so wait() cannot hang
    # waiting for paused stdout/stderr transports to observe EOF.
    await asyncio.gather(discard(process.stdout), discard(process.stderr))
    await process.wait()
