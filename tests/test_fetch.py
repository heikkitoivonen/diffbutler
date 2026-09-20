"""Managed source transport remains explicit, bounded, and credential-safe."""

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import pytest
from tests.test_sync import REF

from diffbutler.sources.fetch import fetch_repository, git_fetch_command, repository_url
from diffbutler.sources.git import SourceError


def test_repository_url_uses_forge_identity() -> None:
    assert repository_url(REF) == "https://github.com/acme/api.git"
    assert (
        repository_url(replace(REF, forge="github:https://forge.example/api/v3"))
        == "https://forge.example/acme/api.git"
    )
    for forge in (
        "gitlab:https://example.com",
        "github:https://token@example.com",
        "github:file:///tmp/repo",
    ):
        with pytest.raises(SourceError):
            repository_url(replace(REF, forge=forge))


@pytest.mark.asyncio
async def test_invalid_revisions_do_not_start_git(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="refresh"):
        await fetch_repository(tmp_path, REF, "--upload-pack=evil", "b" * 40)


@pytest.mark.asyncio
async def test_cancelled_fetch_stops_process_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "git"
    executable.write_text("#!/bin/sh\necho $$ > pid\nexec sleep 60\n")
    executable.chmod(0o700)
    monkeypatch.chdir(tmp_path)

    def find_git(_: str) -> str:
        return str(executable)

    monkeypatch.setattr("diffbutler.sources.fetch.shutil.which", find_git)
    task = asyncio.create_task(git_fetch_command(tmp_path, "fetch"))
    async with asyncio.timeout(5):
        while not (tmp_path / "pid").exists():
            await asyncio.sleep(0.01)
    pid = int((tmp_path / "pid").read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_git_errors_are_actionable_without_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "git"
    executable.write_text("#!/bin/sh\necho secret-token >&2\nexit 1\n")
    executable.chmod(0o700)

    def find_git(_: str) -> str:
        return str(executable)

    monkeypatch.setattr("diffbutler.sources.fetch.shutil.which", find_git)
    with pytest.raises(SourceError, match="Git HTTPS authentication") as error:
        await git_fetch_command(tmp_path, "fetch")
    assert "secret-token" not in str(error.value)
