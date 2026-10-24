"""Inspect local Git metadata without fetching or executing repository code."""

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from diffbutler.domain.forge import repository_parts


@dataclass(frozen=True)
class GitRepository:
    host: str
    repository: str
    path: Path


def remote_identity(value: str) -> tuple[str, str] | None:
    if "://" not in value:
        match = re.fullmatch(r"(?:[^@/\s]+@)?([^:/\s]+):(.+)", value)
        if not match:
            return None
        value = f"ssh://{match[1]}/{match[2]}"
    try:
        url = urlsplit(value)
        if url.scheme not in {"https", "http", "ssh", "git"} or not url.hostname:
            return None
        if url.query or url.fragment:
            return None
        repository = url.path.strip("/").removesuffix(".git")
        repository_parts(repository)
        return url.hostname.lower(), repository
    except ValueError:
        return None


async def discover_repository(directory: Path) -> GitRepository | None:
    executable = shutil.which("git")
    if executable is None:
        return None

    async def git(*arguments: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "-C",
                str(directory),
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            async with asyncio.timeout(10):
                output, _ = await process.communicate()
            return output.decode("utf-8").strip() if process.returncode == 0 else None
        except TimeoutError, UnicodeError:
            return None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    root = await git("rev-parse", "--show-toplevel")
    if not root:
        return None
    remotes = await git("config", "--local", "--no-includes", "--get-regexp", r"^remote\..*\.url$")
    if not remotes:
        return None
    entries = [line.split(maxsplit=1) for line in remotes.splitlines()]
    origins = [entry[1] for entry in entries if len(entry) == 2 and entry[0] == "remote.origin.url"]
    values = origins or [entry[1] for entry in entries if len(entry) == 2]
    identities = {remote_identity(value) for value in values}
    if len(identities) != 1 or None in identities:
        return None
    identity = next(iter(identities))
    assert identity is not None
    return GitRepository(*identity, Path(root))
