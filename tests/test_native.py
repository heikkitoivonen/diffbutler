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

pytestmark = pytest.mark.asyncio
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
