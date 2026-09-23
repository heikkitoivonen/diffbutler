"""Managed source transport remains explicit, bounded, and credential-safe."""

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Literal

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
    assert repository_url(REF, transport="ssh") == "ssh://git@github.com/acme/api.git"
    assert (
        repository_url(
            replace(REF, forge="github:https://forge.example:8443/api/v3"), transport="ssh"
        )
        == "ssh://git@forge.example/acme/api.git"
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
    with pytest.raises(SourceError, match="HTTPS credentials or SSH keys") as error:
        await git_fetch_command(tmp_path, "fetch")
    assert "secret-token" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("https_works,ssh_works", [(True, False), (False, True), (False, False)])
@pytest.mark.parametrize("preferred", ["auto", "https", "ssh"])
async def test_fetch_transports_preserve_pinned_revisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    https_works: bool,
    ssh_works: bool,
    preferred: Literal["auto", "https", "ssh"],
) -> None:
    calls: list[tuple[str, ...]] = []

    async def command(repository: Path, *args: str) -> None:
        assert repository == tmp_path
        calls.append(args)
        if args[0] == "fetch":
            succeeds = https_works if repository_url(REF) in args else ssh_works
            if not succeeds:
                raise SourceError("transport unavailable")

    monkeypatch.setattr("diffbutler.sources.fetch.git_fetch_command", command)
    if https_works or ssh_works:
        result = await fetch_repository(tmp_path, REF, "a" * 40, "b" * 40, transport=preferred)
        assert result == ("https" if https_works else "ssh")
    else:
        with pytest.raises(SourceError, match="transport unavailable") as error:
            await fetch_repository(tmp_path, REF, "a" * 40, "b" * 40, transport=preferred)
        assert "https: transport unavailable" in str(error.value)
        assert "ssh: transport unavailable" in str(error.value)
    assert calls[0] == ("init", "--bare", "--template=")
    first_works = ssh_works if preferred == "ssh" else https_works
    assert len(calls) == (2 if first_works else 3)
    for args in calls[1:]:
        assert args[-3:] == ("a" * 40, "b" * 40, f"refs/pull/{REF.number}/head")
        assert "--no-recurse-submodules" in args
    first = "ssh" if preferred == "ssh" else "https"
    assert calls[1][-4] == repository_url(REF, transport=first)
    if not first_works:
        assert calls[2][-4] == repository_url(REF, transport="https" if first == "ssh" else "ssh")


@pytest.mark.asyncio
async def test_fetch_uses_agent_without_prompts_or_inherited_git_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "git"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "assert os.environ['SSH_AUTH_SOCK'] == '/synthetic/agent'\n"
        "assert os.environ['GIT_ALLOW_PROTOCOL'] == 'https:ssh'\n"
        "assert os.environ['GIT_TERMINAL_PROMPT'] == '0'\n"
        "assert os.environ['GIT_ASKPASS'] == '/bin/false'\n"
        "assert os.environ['GIT_SSH_VARIANT'] == 'ssh'\n"
        "command = os.environ['GIT_SSH_COMMAND']\n"
        "assert 'BatchMode=yes' in command and 'StrictHostKeyChecking=yes' in command\n"
        "assert 'PermitLocalCommand=no' in command\n"
        "assert 'GIT_CONFIG_COUNT' not in os.environ\n"
        "assert 'GITHUB_TOKEN' not in os.environ\n"
    )
    executable.chmod(0o700)

    def find_git(_: str) -> str:
        return str(executable)

    monkeypatch.setattr("diffbutler.sources.fetch.shutil.which", find_git)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/synthetic/agent")
    monkeypatch.setenv("GIT_SSH_COMMAND", "unwanted-command")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-secret")
    await git_fetch_command(tmp_path, "fetch")


@pytest.mark.asyncio
async def test_cancelled_https_fetch_does_not_try_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def command(repository: Path, *args: str) -> None:
        calls.append(args[0])
        if args[0] == "fetch":
            raise asyncio.CancelledError

    monkeypatch.setattr("diffbutler.sources.fetch.git_fetch_command", command)
    with pytest.raises(asyncio.CancelledError):
        await fetch_repository(tmp_path, REF, "a" * 40, "b" * 40)
    assert calls == ["init", "fetch"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output,hint",
    [
        ("Permission denied (publickey).", "SSH key authentication failed"),
        ("Host key verification failed.", "SSH host trust failed"),
        ("REMOTE HOST IDENTIFICATION HAS CHANGED", "SSH host key changed"),
        ("Could not resolve hostname private-host", "Host resolution failed"),
        ("Connection refused", "Connection refused"),
        ("SSL certificate problem", "HTTPS certificate verification failed"),
        ("fatal: couldn't find remote ref refs/pull/123/head", "Requested remote ref"),
        ("unknown private server message", "Unrecognized Git failure"),
    ],
)
async def test_fetch_failure_classification_keeps_raw_output_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    output: str,
    hint: str,
) -> None:
    executable = tmp_path / "git"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if 'init' in sys.argv: sys.exit(0)\n"
        f"sys.stderr.write({output!r} + '\\nsecret-token https://user:password@private-host/repo\\n')\n"
        "sys.stderr.write('x' * 2_000_000)\n"
        "sys.exit(128)\n"
    )
    executable.chmod(0o700)

    def find_git(_: str) -> str:
        return str(executable)

    monkeypatch.setattr("diffbutler.sources.fetch.shutil.which", find_git)
    async with asyncio.timeout(10):
        with pytest.raises(SourceError) as error:
            await fetch_repository(tmp_path, REF, "a" * 40, "b" * 40)
    message = str(error.value)
    assert f"Git fetch failed (exit 128): {hint}" in message
    assert "https:" in message and "ssh:" in message
    assert "https source fetch failed" in caplog.text
    assert "ssh source fetch failed" in caplog.text
    for secret in ("secret-token", "private-host", "user:password", "private server message"):
        assert secret not in message + caplog.text
