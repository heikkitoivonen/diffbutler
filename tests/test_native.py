"""Offline native pipeline acceptance using real subprocesses and disposable Git."""

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from tests.test_sources import git
from tests.test_sync import ACCOUNT, PR, REF, FakeForge

from diffbutler.config.settings import Settings
from diffbutler.core.managed import ManagedReviews, owned_directories, resolve_local
from diffbutler.core.native import foreground_environment
from diffbutler.core.sync import DiffButler
from diffbutler.reviewers.base import ReviewOutput
from diffbutler.reviewers.extraction import extract_output
from diffbutler.reviewers.native import (
    CODEX_OUTPUT,
    CapturedProcess,
    NativeError,
    codex_preset,
    verify_version,
)
from diffbutler.store.database import Database
from diffbutler.store.queue import SQLiteQueueStore

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        sys.platform != "linux", reason="Native M2 requires Linux host/boot identity"
    ),
]
FAKE = Path(__file__).parent / "fixtures" / "fake_codex.py"


async def prepared(tmp_path: Path) -> tuple[Settings, FakeForge]:
    repo = tmp_path / "repository"
    repo.mkdir()
    await git(repo, "init", "--template=")
    (repo / "changed.py").write_text("before\n")
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "base")
    base = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    (repo / "changed.py").write_text("after\n")
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "head")
    head = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    (repo / "changed.py").write_text("dirty unrelated work\n")
    settings = Settings.model_validate(
        {
            "db": {"url": f"sqlite+aiosqlite:///{tmp_path}/native.db"},
            "general": {"workspace_root": str(tmp_path / "managed")},
            "review": {"default_reviewer": "codex"},
            "reviewers": [
                {"id": "codex", "preset": "codex", "command": [sys.executable, str(FAKE)]}
            ],
            "repos": {"acme/api": {"path": str(repo)}},
        }
    )
    forge = FakeForge()
    forge.prs[REF] = replace(PR, base_sha=base, head_sha=head)
    from diffbutler.domain.forge import Discovery, RequestedPR

    forge.discovery = Discovery((RequestedPR(forge.prs[REF], ("user:reader",)),), True)
    return settings, forge


async def test_review_restart_exact_resume_completion_cleanup_and_retained_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Never expose a real native login to the synthetic executable.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-login"))
    monkeypatch.setenv("GH_TOKEN", "synthetic-foreground-only")
    settings, forge = await prepared(tmp_path)
    database = Database(settings.db.url)
    await database.initialize()
    try:
        queue = SQLiteQueueStore(database)
        await DiffButler(queue, forge).sync()
        directories = await owned_directories(settings)
        managed = ManagedReviews(database, settings, directories)
        row = await resolve_local(queue, "acme/api#1", None)
        assert row.tracked.task_id is not None
        assert await managed.store.latest_run(row.tracked.task_id) is None
        run = await managed.review(row)
        assert run.status == "succeeded"
        session = await managed.reviews.get_session(run.session_id)
        assert session.backend_session_id == "01234567-89ab-cdef-0123-456789abcdef"
        result = await managed.findings(row)
        assert result is not None and result.document is not None
        assert result.document.orientation.intent == "Change behavior"
        assert not result.document.findings
        assert (await queue.snapshot(ACCOUNT)).items[0].state == "PENDING_HUMAN_REVIEW"
    finally:
        await database.close()
    database = Database(settings.db.url)
    try:
        managed = ManagedReviews(database, settings, await owned_directories(settings))
        row = await resolve_local(SQLiteQueueStore(database), "acme/api#1", None)
        launch = await managed.prepare_handoff(row)
        execution = CapturedProcess()
        try:
            resumed = await execution.capture(
                launch.argv,
                cwd=launch.cwd,
                env=foreground_environment(launch),
                input=b"",
                maximum=10000,
            )
            assert resumed.exit_code == 0 and b"remembered orientation" in resumed.stdout
        finally:
            await execution.stop_and_wait()
            await managed.lifecycle.release_foreground(launch.token)
        # Resume did not create another AI attempt.
        latest = await managed.store.latest_run(run.task_id)
        assert latest is not None and latest.id == run.id
        await managed.store.mark_approved(run.task_id)
        await managed.cleanup()
        assert not launch.agent_home.exists() and not launch.cwd.exists()
        assert (await managed.reviews.get_session(run.session_id)).status == "cleaned"
        result = await managed.reviews.get_result(run.id)
        assert result is not None and result.document is not None and result.stdout is None
        assert (await SQLiteQueueStore(database).snapshot(ACCOUNT)).items[0].state == "DONE"
    finally:
        await database.close()


async def test_native_capture_overflow_and_cancellation_stop_owned_group(tmp_path: Path) -> None:
    for code, maximum in (("print('x'*100000)", 10), ("import time;time.sleep(60)", 1000)):
        execution = CapturedProcess()
        task = asyncio.create_task(
            execution.capture(
                (sys.executable, "-c", code),
                cwd=tmp_path,
                env={"PATH": os.defpath},
                input=b"",
                maximum=maximum,
            )
        )
        try:
            if maximum == 10:
                with pytest.raises(NativeError, match="byte budget"):
                    await task
            else:
                while execution.spawn is None:
                    await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            await execution.stop_and_wait()
        assert execution.process is not None and execution.process.returncode is not None


async def test_unverified_version_fails_without_running_review(tmp_path: Path) -> None:
    with pytest.raises(NativeError, match="version"):
        await verify_version(
            (sys.executable,), codex_preset(), cwd=tmp_path, env={"PATH": os.defpath}
        )


async def test_codex_tool_events_are_not_assistant_answers() -> None:
    output = ReviewOutput(
        b'{"type":"item.completed","item":{"type":"command_execution","text":"{}"}}\n{"type":"turn.completed"}\n',
        b"",
        0,
    )
    assert extract_output(output, CODEX_OUTPUT).error is not None


def configuration(tmp_path: Path, settings: Settings) -> Path:
    import json

    path = tmp_path / "native.toml"
    path.write_text(
        f"[db]\nurl = {json.dumps(settings.db.url)}\n"
        f"[general]\nworkspace_root = {json.dumps(str(settings.general.workspace_root))}\n"
        '[review]\ndefault_reviewer = "codex"\n'
        '[[reviewers]]\nid = "codex"\npreset = "codex"\n'
        f"command = {json.dumps(settings.reviewers[0].command)}\n"
        f'[repos."acme/api"]\npath = {json.dumps(str(settings.repos["acme/api"].path))}\n'
    )
    return path


async def test_public_cli_review_retry_findings_approval_and_cleanup(tmp_path: Path) -> None:
    import json

    settings, forge = await prepared(tmp_path)
    config = configuration(tmp_path, settings)
    database = Database(settings.db.url)
    await database.initialize()
    try:
        await DiffButler(SQLiteQueueStore(database), forge).sync()
    finally:
        await database.close()

    async def cli(*args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "diffbutler",
            *args,
            "--config",
            str(config),
            env={**os.environ, "CODEX_HOME": str(tmp_path / "empty-login")},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        return stdout.decode()

    assert "No saved review" in await cli("findings", "acme/api#1")
    first = json.loads(await cli("review", "acme/api#1"))
    assert first["status"] == "succeeded"
    second = json.loads(await cli("review", PR.url))
    assert second["status"] == "succeeded"
    assert first["id"] != second["id"] and first["session_id"] == second["session_id"]
    # Public session command in a controlling PTY, after separate CLI process restarts.
    import pty
    import termios

    master, slave = pty.openpty()
    modes = termios.tcgetattr(slave)
    launcher = (
        "import os,sys,fcntl,termios; os.setsid(); "
        "fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])"
    )
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            launcher,
            sys.executable,
            "-m",
            "diffbutler",
            "session",
            "acme/api#1",
            "--config",
            str(config),
            stdin=slave,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
            assert process.returncode == 0, stderr.decode()
            assert b"remembered orientation" in stdout
            assert termios.tcgetattr(slave) == modes
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    finally:
        os.close(master)
        os.close(slave)
    assert "Change behavior" in await cli("findings", "acme/api#1")
    assert "1 succeeded, 0 pending, 0 running, 0 failed" in await cli("approved", "acme/api#1")
    assert "1 succeeded" in await cli("cleanup")
    assert "Change behavior" in await cli("findings", "acme/api#1")
    assert not json.loads(await cli("ls", "--json"))
    repository = settings.repos["acme/api"].path
    assert repository is not None
    assert (repository / "changed.py").read_text() == "dirty unrelated work\n"


@pytest.mark.parametrize("uncertain", [False, True])
async def test_shared_handoff_preserves_uncertain_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertain: bool
) -> None:
    from diffbutler.core.managed import handoff_configured
    from diffbutler.domain.lifecycle import ForegroundExitUncertain

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-login"))
    settings, forge = await prepared(tmp_path)
    config = configuration(tmp_path, settings)
    database = Database(settings.db.url)
    await database.initialize()
    try:
        queue = SQLiteQueueStore(database)
        await DiffButler(queue, forge).sync()
        managed = ManagedReviews(database, settings, await owned_directories(settings))
        row = await resolve_local(queue, "acme/api#1", None)
        run = await managed.review(row)
        error = ForegroundExitUncertain if uncertain else OSError
        with pytest.raises(error):
            async with handoff_configured(config, "acme/api#1", None):
                # A concurrent local completion must defer deletion until verified exit.
                await managed.store.mark_approved(run.task_id)
                assert (await managed.cleanup()).pending == 1
                raise error("synthetic launch/exit failure")
        status = await managed.leases.cleanup_status()
        assert status.pending == int(uncertain)
        assert status.succeeded == int(not uncertain)
        assert bool(await managed.leases.held_operations()) == uncertain
    finally:
        await database.close()


async def test_failed_resume_version_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from diffbutler.core.native import prepare_native_handoff

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-login"))
    settings, forge = await prepared(tmp_path)
    database = Database(settings.db.url)
    await database.initialize()
    try:
        queue = SQLiteQueueStore(database)
        await DiffButler(queue, forge).sync()
        managed = ManagedReviews(database, settings, await owned_directories(settings))
        run = await managed.review(await resolve_local(queue, "acme/api#1", None))
        with pytest.raises(NativeError, match="version"):
            await prepare_native_handoff(
                managed.store,
                managed.leases,
                managed.directories,
                run.session_id,
                (sys.executable,),
            )
        assert not await managed.leases.held_operations()
    finally:
        await database.close()


async def test_managed_cleanup_failure_and_interruption_are_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace as changed
    from datetime import timedelta
    from uuid import uuid4

    from sqlalchemy import update
    from tests.test_process_recovery import Host

    from diffbutler.domain.lifecycle import CleanupSession
    from diffbutler.store.models import CleanupJob, utc_now

    class InterruptedCleaner:
        async def cleanup(self, session: CleanupSession) -> None:
            raise asyncio.CancelledError

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-login"))
    settings, forge = await prepared(tmp_path)
    database = Database(settings.db.url)
    await database.initialize()
    try:
        queue = SQLiteQueueStore(database)
        await DiffButler(queue, forge).sync()
        managed = ManagedReviews(database, settings, await owned_directories(settings))
        run = await managed.review(await resolve_local(queue, "acme/api#1", None))
        await managed.store.mark_approved(run.task_id)
        host = Host()
        managed.recovery.host = host
        cleaner = managed.lifecycle.cleaner
        managed.lifecycle.cleaner = InterruptedCleaner()
        with pytest.raises(asyncio.CancelledError):
            await managed.cleanup()
        assert (await managed.leases.cleanup_status()).running == 1
        held = await managed.leases.held_operations()
        assert len(held) == 1 and held[0].kind == "cleanup"
        assert await managed.processes.get_launch(held[0].token) is not None
        assert not await managed.recovery.recover_rebooted()
        host.identity = changed(host.identity, boot_id=uuid4())
        assert await managed.recovery.recover_rebooted() == (held[0].token,)
        assert (await managed.leases.cleanup_status()).failed == 1
        async with database.sessions() as db:
            await db.execute(
                update(CleanupJob).values(next_retry_at=utc_now() - timedelta(seconds=1))
            )
            await db.commit()
        managed.lifecycle.cleaner = cleaner
        status = await managed.cleanup()
        assert status.succeeded == 1 and status.failed == 0
    finally:
        await database.close()
