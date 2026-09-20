"""Explicit, bounded GitHub source preparation without checking out project code."""

import asyncio
import logging
import os
import re
import shutil
import signal
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from diffbutler.domain.forge import PRRef
from diffbutler.sources.git import SourceError


def repository_url(ref: PRRef) -> str:
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
    return f"https://{host}/{ref.namespace}/{ref.repository}.git"


async def git_fetch_command(repository: Path, *args: str) -> None:
    executable = shutil.which("git")
    if executable is None:
        raise SourceError("Git is unavailable; install Git and retry the review")
    # Allow the user's Git HTTPS credential helpers, but no inherited Git command overrides.
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "XDG_CONFIG_HOME", "LANG")
        if key in os.environ
    }
    env.update(
        {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "GIT_ALLOW_PROTOCOL": "https"}
    )
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
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    )

    async def settle() -> None:
        process = await spawn
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()

    try:
        async with asyncio.timeout(120):
            process = await asyncio.shield(spawn)
            if await process.wait() != 0:
                raise SourceError(
                    "Could not prepare PR commits. Check Git HTTPS authentication and network "
                    "access, then refresh the PR and retry."
                )
    except TimeoutError:
        raise SourceError("Fetching PR commits timed out; check network access and retry") from None
    except OSError:
        raise SourceError(
            "Could not start Git; check installation and repository permissions"
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


async def fetch_repository(repository: Path, ref: PRRef, base_sha: str, head_sha: str) -> None:
    for sha in (base_sha, head_sha):
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha) is None:
            raise SourceError("PR commit information is incomplete; refresh the PR and retry")
    remote = repository_url(ref)
    logging.getLogger(__name__).info("Preparing managed Git repository")
    await git_fetch_command(repository, "init", "--bare", "--template=")
    logging.getLogger(__name__).info("Fetching pinned PR commits")
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
