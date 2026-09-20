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
from diffbutler.core.native import background_environment, foreground_environment
from diffbutler.core.sync import DiffButler
from diffbutler.reviewers.base import ReviewOutput
from diffbutler.reviewers.extraction import extract_output
from diffbutler.reviewers.native import (
    CLAUDE_OUTPUT,
    CODEX_OUTPUT,
    CapturedProcess,
    NativeError,
    codex_preset,
    native_preset,
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


@pytest.fixture(autouse=True)
def isolated_native_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "synthetic-native-login"))
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "synthetic-claude-login"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


async def prepared(tmp_path: Path, preset: str = "codex") -> tuple[Settings, FakeForge]:
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
            "review": {"default_reviewer": preset},
            "reviewers": [
                {
                    "id": preset,
                    "preset": preset,
                    "command": [sys.executable, str(FAKE.with_name(f"fake_{preset}.py"))],
                }
            ],
            "repos": {"acme/api": {"path": str(repo)}},
        }
    )
    forge = FakeForge()
    forge.prs[REF] = replace(PR, base_sha=base, head_sha=head)
    from diffbutler.domain.forge import Discovery, RequestedPR

    forge.discovery = Discovery((RequestedPR(forge.prs[REF], ("user:reader",)),), True)
    return settings, forge


@pytest.mark.parametrize("preset", ["codex", "claude"])
async def test_review_restart_exact_resume_completion_cleanup_and_retained_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preset: str,
) -> None:
    # Never expose a real native login to the synthetic executable.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-login"))
    monkeypatch.setenv("GH_TOKEN", "synthetic-foreground-only")
    login = tmp_path / "user-login"
    login.mkdir()
    home_key = "CLAUDE_CONFIG_DIR" if preset == "claude" else "CODEX_HOME"
    credential = login / (".credentials.json" if preset == "claude" else "auth.json")
    credential.write_text("synthetic-auth")
    (login / "settings.json").write_text("synthetic-user-settings")
    monkeypatch.setenv(home_key, str(login))
    settings, forge = await prepared(tmp_path, preset)
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
        assert (launch.agent_home / credential.name).resolve() == credential
        assert not (launch.agent_home / "settings.json").exists()
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
        assert credential.read_text() == "synthetic-auth"
        assert (login / "settings.json").read_text() == "synthetic-user-settings"
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
        f'[review]\ndefault_reviewer = "{settings.review.default_reviewer}"\n'
        f'[[reviewers]]\nid = "{settings.reviewers[0].id}"\n'
        f'preset = "{settings.reviewers[0].preset}"\n'
        f"command = {json.dumps(settings.reviewers[0].command)}\n"
        f'[repos."acme/api"]\npath = {json.dumps(str(settings.repos["acme/api"].path))}\n'
    )
    return path


@pytest.mark.parametrize("preset", ["codex", "claude"])
async def test_public_cli_review_retry_findings_approval_and_cleanup(
    tmp_path: Path, preset: str
) -> None:
    import json

    settings, forge = await prepared(tmp_path, preset)
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


async def test_dashboard_native_review_return_reconciles_feedback_and_keeps_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from contextlib import nullcontext
    from datetime import UTC, datetime

    from textual.widgets import DataTable

    from diffbutler.config.settings import Forge
    from diffbutler.core.dashboard import HistoryFilter, detail_configured, history_configured
    from diffbutler.domain.sync import ReviewActivity, ReviewEvent
    from diffbutler.ui.tui.dashboard import Dashboard

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-login"))
    settings, forge = await prepared(tmp_path)
    settings.forges = [
        Forge(
            kind="github",
            base_url="https://api.github.com",
            token_env="UNUSED",
            login="reader",
            include_team_requests=False,
        )
    ]
    config = configuration(tmp_path, settings)
    with config.open("a") as stream:
        stream.write(
            '\n[[forges]]\nkind="github"\nbase_url="https://api.github.com"\ntoken_env="UNUSED"\nlogin="reader"\ninclude_team_requests=false\n'
        )

    async def open_fake(_: Forge) -> FakeForge:
        return forge

    monkeypatch.setattr("diffbutler.core.application.open_forge", open_fake)
    database = Database(settings.db.url)
    await database.initialize()
    queue = SQLiteQueueStore(database)
    await DiffButler(queue, forge).sync()
    seen: list[tuple[str, ...]] = []

    async def foreground(
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        started: Callable[[int], Awaitable[None]] | None = None,
    ) -> int:
        # Real synthetic native process, headless terminal attachment is the only fake.
        seen.append(tuple(argv))
        assert "wait for the human's question" in argv[-1]
        assert "on-request" in argv and "danger-full-access" in argv
        assert "read-only" not in argv
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if started:
            await started(process.pid)
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert b"remembered orientation" in stdout
        forge.activity = ReviewActivity(
            ACCOUNT,
            reviews=(
                ReviewEvent(
                    "review:submitted",
                    datetime.now(UTC),
                    "reader",
                    "CHANGES_REQUESTED",
                    forge.prs[REF].head_sha,
                    "Synthetic human-instructed feedback",
                ),
            ),
        )
        return 0

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.run_foreground", foreground)
    app = Dashboard(config, settings, offline=True)
    monkeypatch.setattr(app, "suspend", nullcontext)
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(100):
                await asyncio.sleep(0.05)
                if not app.review_tasks:
                    break
            assert not app.review_tasks
            await pilot.pause()
            current = (await queue.snapshot(ACCOUNT)).items[0]
            assert current.state == "PENDING_HUMAN_REVIEW"
            task_id = current.task_id
            assert task_id is not None
            await pilot.press("v")
            for _ in range(100):
                await asyncio.sleep(0.05)
                current = (await queue.snapshot(ACCOUNT)).items[0]
                if (
                    seen
                    and not current.refresh_pending
                    and current.state == "PENDING_AUTHOR_CHANGES"
                ):
                    break
            await pilot.pause()
            assert current.state == "PENDING_AUTHOR_CHANGES" and not current.refresh_pending
            assert len(seen) == 1
            await pilot.press("m")
            await pilot.pause()
            assert app.query_one(DataTable[str]).row_count == 0
        history = await history_configured(config, HistoryFilter())
        assert len(history) == 1 and history[0].cleanup_status == "succeeded"
        assert len(history[0].runs) == 1
        detail = await detail_configured(config, task_id)
        assert detail.results[0].document is not None
        assert detail.results[0].document.orientation.intent == "Change behavior"
        assert detail.cycle.receipts[0].body == "Synthetic human-instructed feedback"
        assert detail.results[0].stdout is None
    finally:
        await database.close()


async def test_public_dashboard_native_handoff_restores_controlling_terminal(
    tmp_path: Path,
) -> None:
    from tests.test_handoff_pty import Terminal

    settings, forge = await prepared(tmp_path)
    config = configuration(tmp_path, settings)
    database = Database(settings.db.url)
    await database.initialize()
    try:
        await DiffButler(SQLiteQueueStore(database), forge).sync()
    finally:
        await database.close()
    terminal = Terminal(
        tmp_path,
        app_argv=[
            sys.executable,
            "-m",
            "diffbutler",
            "dashboard",
            "--offline",
            "--config",
            str(config),
        ],
    )
    try:
        await asyncio.to_thread(terminal.wait, lambda: b"pending" in terminal.output)
        terminal.send(b"r")
        await asyncio.to_thread(terminal.wait, lambda: b"AI Status: succeeded" in terminal.output)
        terminal.send(b"v")
        await asyncio.to_thread(terminal.wait, lambda: b"remembered orientation" in terminal.output)
        await asyncio.to_thread(terminal.wait, lambda: b"Agent exited 0" in terminal.output)
        assert os.tcgetpgrp(terminal.master) == terminal.process.pid
        # Returning leaves a resumable conversation, no approval and no extra AI run.
        terminal.send(b"v")
        await asyncio.to_thread(
            terminal.wait, lambda: terminal.output.count(b"remembered orientation") == 2
        )
        await asyncio.to_thread(
            terminal.wait, lambda: os.tcgetpgrp(terminal.master) == terminal.process.pid
        )
        await asyncio.to_thread(terminal.quit)
    finally:
        terminal.close()
    database = Database(settings.db.url)
    await database.initialize()
    try:
        from diffbutler.store.dashboard import SQLiteDashboardStore

        cycles = await SQLiteDashboardStore(database).cycles()
        assert len(cycles) == 1 and len(cycles[0].runs) == 1
        assert cycles[0].state == "PENDING_HUMAN_REVIEW"
    finally:
        await database.close()


async def test_paused_dispatch_and_cancelling_queued_review(tmp_path: Path) -> None:
    from diffbutler.store.dashboard import SQLiteDashboardStore

    settings, forge = await prepared(tmp_path)
    database = Database(settings.db.url)
    await database.initialize()
    managed = ManagedReviews(database, settings, await owned_directories(settings))
    try:
        queue = SQLiteQueueStore(database)
        await DiffButler(queue, forge).sync()
        row = await resolve_local(queue, "acme/api#1", None)
        assert row.tracked.task_id is not None
        await SQLiteDashboardStore(database).set_preference("reviews_paused", "true")
        task = asyncio.create_task(managed.review(row))
        run = None
        try:
            for _ in range(100):
                await asyncio.sleep(0.01)
                run = await managed.store.latest_run(row.tracked.task_id)
                if run is not None:
                    break
            assert run is not None and run.status == "queued"
            assert not await managed.leases.held_operations()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert (await managed.store.get_run(run.id)).status == "cancelled"
        assert (await queue.snapshot(ACCOUNT)).items[0].state == "PENDING_AI_REVIEW"
    finally:
        await database.close()


@pytest.mark.parametrize("change", ["error", "subtype", "missing", "conflict", "truncated"])
async def test_claude_rejects_failed_or_incomplete_results(change: str) -> None:
    import json

    identity = "01234567-89ab-cdef-0123-456789abcdef"
    init = {"type": "system", "subtype": "init", "session_id": identity}
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": identity,
        "result": "{}",
    }
    if change == "error":
        result["is_error"] = True
    elif change == "subtype":
        result["subtype"] = "error_max_turns"
    elif change == "missing":
        del result["session_id"]
    elif change == "conflict":
        result["session_id"] = "aaaaaaaa-89ab-cdef-0123-456789abcdef"
    events = [init] if change == "truncated" else [init, result]
    output = ReviewOutput("\n".join(map(json.dumps, events)).encode(), b"", 0)
    assert extract_output(output, CLAUDE_OUTPUT).error is not None


async def test_claude_environment_and_resume_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    monkeypatch.setenv("GH_TOKEN", "synthetic-forge")
    monkeypatch.setenv("CODEX_API_KEY", "synthetic-codex")
    env = background_environment(tmp_path, "claude")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    assert env["ANTHROPIC_API_KEY"] == "synthetic"
    assert "GH_TOKEN" not in env and "CODEX_API_KEY" not in env
    preset = native_preset("claude")
    identity = "01234567-89ab-cdef-0123-456789abcdef"
    resumed = preset.argv(("claude",), identity)
    assert resumed[:-2] == preset.argv(("claude",), None)
    assert resumed[-2:] == ("--resume", identity)
    foreground = preset.argv(("claude",), identity, foreground=True)
    assert "manual" in foreground and "dontAsk" not in foreground
    assert foreground[foreground.index("--tools") + 1] == "default"
    assert not {"--restricted", "--safe-mode", "--strict-mcp-config", "--mcp-config"}.intersection(
        foreground
    )
    assert "--restricted" in resumed and "Read,Glob,Grep" in resumed
    with pytest.raises(NativeError, match="identity"):
        preset.argv(("claude",), "--unsafe")


async def test_claude_checkpoints_init_before_completion(tmp_path: Path) -> None:
    seen: list[str] = []
    ready = asyncio.Event()

    async def checkpoint(identity: str) -> None:
        seen.append(identity)
        ready.set()

    execution = CapturedProcess()
    code = (
        "import json,time; "
        'print(json.dumps({"type":"system","subtype":"status"}),flush=True); '
        'print(json.dumps({"type":"system","subtype":"init",'
        '"session_id":"01234567-89ab-cdef-0123-456789abcdef"}),flush=True); '
        "time.sleep(60)"
    )
    task = asyncio.create_task(
        execution.capture(
            (sys.executable, "-c", code),
            cwd=tmp_path,
            env={"PATH": os.defpath},
            input=b"",
            maximum=1000,
            checkpoint=checkpoint,
            output_format=CLAUDE_OUTPUT,
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        assert seen == ["01234567-89ab-cdef-0123-456789abcdef"]
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await execution.stop_and_wait()
