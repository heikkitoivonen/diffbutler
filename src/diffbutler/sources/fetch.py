"""Explicit, bounded GitHub source preparation without checking out project code."""

import asyncio
import logging
import os
import re
import shutil
import signal
from contextlib import suppress
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from diffbutler.domain.forge import PRRef
from diffbutler.sources.git import SourceError


def repository_url(ref: PRRef, *, transport: Literal["https", "ssh"] = "https") -> str:
    """Derive the clone URL from the persisted forge identity, never a PR body/URL."""
    if not ref.forge.startswith("github:"):
        raise SourceError(
            "Automatic source preparation currently supports GitHub repositories only"
        )
    url = urlsplit(ref.forge.removeprefix("github:"))
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path.rstrip("/") not in {"", "/api/v3"}
    ):
        raise SourceError("Configure a valid HTTPS GitHub API address before preparing sources")
    host = "github.com" if url.netloc == "api.github.com" else url.netloc
    if transport == "ssh":
        # API ports are not SSH ports; SSH config supplies custom ports and keys.
        host = "github.com" if url.hostname == "api.github.com" else url.hostname
        if ":" in host:
            host = f"[{host}]"
        return f"ssh://git@{host}/{ref.namespace}/{ref.repository}.git"
    return f"https://{host}/{ref.namespace}/{ref.repository}.git"


def _failure_hint(stderr: bytes) -> str:
    """Classify output without copying credentials, paths, or server text into logs."""
    output = stderr.lower()
    hints = (
        (
            b"host key verification failed",
            "SSH host trust failed; verify the host outside DiffButler.",
        ),
        (
            b"remote host identification has changed",
            "SSH host key changed; verify it with your administrator.",
        ),
        (
            b"permission denied (publickey",
            "SSH key authentication failed; check the SSH agent and SSH configuration.",
        ),
        (
            b"could not resolve",
            "Host resolution failed; check DNS, VPN, and SSH host configuration.",
        ),
        (b"connection refused", "Connection refused; check the SSH port, VPN, and firewall."),
        (b"connection timed out", "Connection timed out; check VPN, firewall, and network access."),
        (
            b"ssl certificate problem",
            "HTTPS certificate verification failed; check the corporate CA configuration.",
        ),
        (
            b"authentication failed",
            "HTTPS authentication failed; check Git credential helpers and repository access.",
        ),
        (
            b"could not read username",
            "HTTPS credentials unavailable; configure a Git credential helper or use SSH.",
        ),
        (
            b"repository not found",
            "Repository unavailable; check repository access and the configured identity.",
        ),
        (
            b"couldn't find remote ref",
            "Requested remote ref is unavailable; refresh the PR and retry.",
        ),
        (
            b"not our ref",
            "Requested commit is unavailable on the remote; refresh the PR and retry.",
        ),
        (b"no space left on device", "Local disk is full; free space and retry."),
        (
            b"permission denied",
            "Permission denied; check local repository permissions and remote access.",
        ),
    )
    for marker, hint in hints:
        if marker in output:
            return hint
    return (
        "Unrecognized Git failure. Check Git HTTPS credentials or SSH keys, SSH host trust, "
        "and network access; reproduce the fetch outside DiffButler for raw Git output."
    )


async def git_fetch_command(repository: Path, *args: str, local: bool = False) -> None:
    operation = args[0] if args and args[0] in {"init", "fetch", "update-ref"} else "command"
    executable = shutil.which("git")
    if executable is None:
        raise SourceError("Git is unavailable; install Git and retry the review")
    # Use existing HTTPS helpers and SSH agents/config, without inherited Git overrides.
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "XDG_CONFIG_HOME", "LANG", "SSH_AUTH_SOCK")
        if key in os.environ
    }
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_ALLOW_PROTOCOL": "file" if local else "https:ssh",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "
            "-o PermitLocalCommand=no",
            "GIT_SSH_VARIANT": "ssh",
            "LC_ALL": "C",
        }
    )
    if local:
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            executable,
            "-C",
            str(repository),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "-c",
            "http.followRedirects=false",
            *args,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    )

    diagnostic: asyncio.Task[bytes] | None = None

    async def collect(stream: asyncio.StreamReader) -> bytes:
        # Drain everything, retaining only a bounded prefix for classification.
        output = bytearray()
        while chunk := await stream.read(65536):
            output.extend(chunk[: max(0, 65536 - len(output))])
        return bytes(output)

    async def settle() -> None:
        process = await spawn
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        if diagnostic is not None:
            await diagnostic
        elif process.stderr is not None:
            await collect(process.stderr)
        await process.wait()

    try:
        async with asyncio.timeout(120):
            process = await asyncio.shield(spawn)
            assert process.stderr is not None
            diagnostic = asyncio.create_task(collect(process.stderr))
            stderr = await asyncio.shield(diagnostic)
            status = await process.wait()
            if status != 0:
                raise SourceError(
                    f"Git {operation} failed (exit {status}): {_failure_hint(stderr)}"
                )
    except TimeoutError:
        raise SourceError(
            f"Git {operation} timed out after 120s; check network access and retry"
        ) from None
    except OSError as exc:
        raise SourceError(
            f"Could not start Git {operation} (errno {exc.errno}); "
            "check installation and repository permissions"
        ) from None
    finally:
        # Do not release session ownership until even a cancelled spawn has settled.
        cleanup = asyncio.create_task(settle())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
            except OSError:
                break
        with suppress(OSError):
            cleanup.result()
        if cancelled:
            raise asyncio.CancelledError


async def fetch_repository(
    repository: Path,
    ref: PRRef,
    base_sha: str,
    head_sha: str,
    *,
    transport: Literal["auto", "https", "ssh"] = "auto",
) -> Literal["https", "ssh"]:
    for sha in (base_sha, head_sha):
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha) is None:
            raise SourceError("PR commit information is incomplete; refresh the PR and retry")
    transports: tuple[Literal["https", "ssh"], ...] = (
        ("ssh", "https") if transport == "ssh" else ("https", "ssh")
    )
    remotes = tuple(repository_url(ref, transport=item) for item in transports)
    logging.getLogger(__name__).info("Preparing managed Git repository")
    await git_fetch_command(repository, "init", "--bare", "--template=")
    logging.getLogger(__name__).info("Fetching pinned PR commits")
    failures: list[str] = []
    for index, remote in enumerate(remotes):
        logging.getLogger(__name__).info("Attempting %s source fetch", transports[index])
        try:
            await git_fetch_command(
                repository,
                "fetch",
                "--depth=1",
                "--no-tags",
                "--no-recurse-submodules",
                "--",
                remote,
                base_sha,
                head_sha,
                f"refs/pull/{ref.number}/head",
            )
            logging.getLogger(__name__).info("Pinned PR commits fetched")
            return transports[index]
        except SourceError as exc:
            failures.append(f"{transports[index]}: {exc}")
            logging.getLogger(__name__).warning(
                "%s source fetch failed: %s", transports[index], exc
            )
            if index == len(remotes) - 1:
                raise SourceError("Could not prepare PR commits. " + " | ".join(failures)) from None
            logging.getLogger(__name__).info(
                "%s source fetch failed; trying %s", transports[index], transports[index + 1]
            )
    raise AssertionError("No source transport attempted")
