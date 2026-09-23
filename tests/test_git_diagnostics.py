"""Opt-in source diagnostics preserve evidence without logging source or credentials."""

import asyncio
import logging
import sys
from pathlib import Path

import pytest

from diffbutler.sources.diagnostics import GitTrace
from diffbutler.sources.fetch import git_fetch_command
from diffbutler.sources.git import GitSourceReader, SourceError, SourceLimits


def test_git_trace_redacts_credentials_and_escapes_controls(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="diffbutler.sources.test")
    monkeypatch.setenv("WORK_TOKEN", "custom-opaque-credential")
    monkeypatch.setenv("HTTPS_PROXY", "https://user:proxy-password@proxy.invalid")
    trace = GitTrace(
        logging.getLogger("diffbutler.sources.test"),
        ["git", "-C", "/managed/repo", "fetch", "a" * 40],
        {"HOME": "/home/test"},
    )
    trace.capture(
        b"fatal: a useful unknown server error\n"
        b"https://username:url-password@host/repo?access_token=query-secret\n"
        b"Authorization: Bearer auth-secret\npassword=short\n"
        b"custom-opaque-credential ghp_12345678901234567890\n"
        b"\x1b[31mcontrol\r\n"
        b"-----BEGIN PRIVATE KEY-----\npartial-key-secret"
    )
    trace.finish(128, "exited")
    content = caplog.text
    assert "a useful unknown server error" in content
    assert "/managed/repo" in content and "a" * 40 in content
    assert trace.command_id in content and "128" in content
    assert "HTTPS_PROXY" in content and "proxy.invalid" not in content
    assert "\\u001b" in content and "\x1b" not in content
    for secret in (
        "url-password",
        "query-secret",
        "auth-secret",
        "short",
        "custom-opaque-credential",
        "ghp_12345678901234567890",
        "partial-key-secret",
    ):
        assert secret not in content


@pytest.mark.asyncio
@pytest.mark.parametrize("debug", [False, True])
async def test_fetch_debug_records_actual_command_and_bounded_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    debug: bool,
) -> None:
    caplog.set_level(logging.DEBUG if debug else logging.INFO, logger="diffbutler.sources")
    executable = tmp_path / "git"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "sys.stdout.write('PRIVATE_SOURCE_STDOUT')\n"
        "sys.stderr.write('fatal: unknown diagnostic detail\\n' + 'x' * 100000)\n"
        "sys.exit(128)\n"
    )
    executable.chmod(0o700)

    def find_git(_: str) -> str:
        return str(executable)

    monkeypatch.setattr("diffbutler.sources.fetch.shutil.which", find_git)
    with pytest.raises(SourceError) as error:
        await git_fetch_command(tmp_path, "fetch", "a" * 40)
    assert "unknown diagnostic detail" not in str(error.value)
    assert "PRIVATE_SOURCE_STDOUT" not in caplog.text
    assert ("unknown diagnostic detail" in caplog.text) == debug
    if debug:
        assert str(tmp_path) in caplog.text and "a" * 40 in caplog.text
        assert '"stderr_truncated": "True"' in caplog.text
        assert len(caplog.text) < 75000


@pytest.mark.asyncio
async def test_cancelled_fetch_logs_partial_stderr_and_settles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="diffbutler.sources")
    executable = tmp_path / "git"
    marker = tmp_path / "started"
    executable.write_text(
        f"#!{sys.executable}\nimport sys, time\nfrom pathlib import Path\n"
        "sys.stderr.write('waiting for network\\n'); sys.stderr.flush()\n"
        f"Path({str(marker)!r}).touch()\ntime.sleep(60)\n"
    )
    executable.chmod(0o700)

    def find_git(_: str) -> str:
        return str(executable)

    monkeypatch.setattr("diffbutler.sources.fetch.shutil.which", find_git)
    task = asyncio.create_task(git_fetch_command(tmp_path, "fetch"))
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "waiting for network" in caplog.text
    assert "interrupted" in caplog.text


@pytest.mark.asyncio
async def test_local_source_debug_never_logs_blob_stdout(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="diffbutler.sources")
    executable = tmp_path / "git"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "sys.stdout.write('PRIVATE_BLOB'); sys.stderr.write('local object diagnostic')\n"
    )
    executable.chmod(0o700)
    reader = GitSourceReader(tmp_path, str(executable), SourceLimits(), {})
    assert await reader._git("cat-file", "blob", "a" * 40) == b"PRIVATE_BLOB"  # pyright: ignore[reportPrivateUsage]
    assert "PRIVATE_BLOB" not in caplog.text
    assert "local object diagnostic" in caplog.text
