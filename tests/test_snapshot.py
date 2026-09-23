"""Full snapshots stream unique objects without per-file Git subprocesses."""

import asyncio
import logging
import os
import sys
from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest
from tests.test_sources import git, repository_at_two_commits

from diffbutler.core.snapshot import write_snapshot
from diffbutler.core.workspaces import materialize
from diffbutler.domain.review import Side
from diffbutler.sources.git import GitSourceReader, SourceError, SourceLimits


@pytest.mark.asyncio
async def test_snapshot_batches_and_deduplicates_blobs_preserving_full_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo, _, _ = await repository_at_two_commits(tmp_path)
    secret = b"ghp_12345678901234567890"
    for number in range(200):
        (repo / f"unchanged-{number}.txt").write_bytes(secret)
    (repo / "binary.dat").write_bytes(b"\x00\xff\nblob\n")
    (repo / "snapshot-link").symlink_to("/outside/target")
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "snapshot fixtures")
    base = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    (repo / "changed.py").write_text("new content\n")
    await git(repo, "commit", "-am", "change")
    head = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    commands: list[tuple[str, ...]] = []
    spawn = asyncio.create_subprocess_exec

    async def track(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        commands.append(argv)
        return await spawn(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track)
    caplog.set_level(logging.INFO, logger="diffbutler.core.snapshot")
    progress: list[str] = []
    destination = tmp_path / "snapshot"
    await write_snapshot(reader, destination, progress=progress.append)
    assert len(commands) == 1 and commands[0][-2:] == ("cat-file", "--batch")
    total = sum(len(reader.paths(side)) for side in Side)
    assert f"{total}/{total} files" in progress[-1]
    assert "masked=1" in caplog.text
    for side in Side:
        root = destination / side.value
        assert sorted(
            p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
        ) == list(reader.paths(side))
        assert (root / "binary.dat").read_bytes() == b"\x00\xff\nblob\n"
        assert not (root / "snapshot-link").is_symlink()
        assert (root / "snapshot-link").read_text() == "/outside/target"
        assert (root / "unchanged-0.txt").read_bytes() == b"*" * len(secret)
        assert (root / "unchanged-0.txt").stat().st_mode & 0o777 == 0o600
    (destination / "base/unchanged-0.txt").write_text("edited")
    assert (destination / "head/unchanged-0.txt").read_bytes() == b"*" * len(secret)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "behavior", ["missing", "wrong-id", "wrong-kind", "truncated", "boundary", "timeout", "cancel"]
)
async def test_batch_failures_and_cancellation_reap_process_and_never_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    behavior: str,
) -> None:
    repo, base, head = await repository_at_two_commits(tmp_path)
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    executable = tmp_path / "fake-git"
    code = {
        "missing": "print(oid + ' missing', flush=True)",
        "wrong-id": "print('wrong blob 0', flush=True)",
        "wrong-kind": "print(oid + ' commit 0', flush=True)",
        "truncated": "print(oid + ' blob 100', flush=True)",
        "boundary": "sys.stdout.write(oid + ' blob 1\\nx!'); sys.stdout.flush()",
        "timeout": "time.sleep(60)",
        "cancel": "time.sleep(60)",
    }[behavior]
    executable.write_text(
        f"#!{sys.executable}\nimport sys, time\n"
        "oid = sys.stdin.readline().strip()\n"
        "sys.stderr.write('private diagnostic' + 'x' * 200000); sys.stderr.flush()\n" + code + "\n"
    )
    executable.chmod(0o700)
    monkeypatch.setattr(reader, "_executable", str(executable))
    monkeypatch.setattr(
        reader, "_limits", SourceLimits(timeout_s=0.3 if behavior == "timeout" else 5)
    )
    created: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def track(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*argv, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track)
    destination = tmp_path / "published"
    task = asyncio.create_task(materialize(reader, destination))
    async with asyncio.timeout(10):
        if behavior == "cancel":
            while not created:
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(SourceError) as error:
                await task
            assert "private diagnostic" not in str(error.value)
    assert not destination.exists()
    assert len(created) == 1 and created[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(created[0].pid, 0)


@pytest.mark.asyncio
async def test_batch_early_close_reaps_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = await repository_at_two_commits(tmp_path)
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    created: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def track(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*argv, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track)
    async with aclosing(reader.read_blobs()) as blobs:
        await anext(blobs)
    assert len(created) == 1 and created[0].returncode is not None


@pytest.mark.asyncio
async def test_batch_cancel_during_spawn_waits_for_process_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, head = await repository_at_two_commits(tmp_path)
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    created: list[asyncio.subprocess.Process] = []
    release = asyncio.Event()
    started = asyncio.Event()
    spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*argv, **kwargs)
        created.append(process)
        started.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(materialize(reader, tmp_path / "snapshot"))
    async with asyncio.timeout(10):
        await started.wait()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert created[0].returncode is not None
