"""Managed native execution and exact conversation handoff preparation."""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from diffbutler.core.process import ProcessRecovery
from diffbutler.core.repositories import RepositoryCache
from diffbutler.core.resources import provision_directory, save_json
from diffbutler.core.workspaces import SnapshotWorkspaces, materialize, materialize_checkout
from diffbutler.domain.lifecycle import CleanupSession
from diffbutler.domain.resources import OwnedDirectory
from diffbutler.domain.review import RunClaim
from diffbutler.reviewers.base import ReviewContext, ReviewOutput
from diffbutler.reviewers.native import CapturedProcess, NativeError, native_preset
from diffbutler.reviewers.prompt import render_git_prompt
from diffbutler.reviewers.resources import OwnedDirectories, OwnedSessionCleaner, settled_io
from diffbutler.sources.git import GitSourceReader
from diffbutler.store.base import LifecycleStore, ProcessStore, ReviewStore


def background_environment(agent_home: Path, preset_name: str = "codex") -> dict[str, str]:
    auth_keys = (
        ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
        if preset_name == "claude"
        else ("CODEX_API_KEY",)
    )
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", *auth_keys)
        if key in os.environ
    }
    env["CLAUDE_CONFIG_DIR" if preset_name == "claude" else "CODEX_HOME"] = str(agent_home)
    return env


async def session_directory(
    store: ReviewStore, directories: OwnedDirectories, session_id: UUID, preset_name: str = "codex"
) -> Path:
    resources = await store.session_resources(session_id)
    if (
        len(resources) != 1
        or resources[0].root_id != directories.root_id
        or resources[0].purpose != f"{preset_name}_home"
    ):
        raise NativeError("Managed native session workspace is unavailable")
    path = directories.path / resources[0].name
    if path.is_symlink() or not path.is_dir():
        raise NativeError("Managed native session workspace is unavailable")
    return path


async def ensure_session_directory(
    store: ReviewStore,
    directories: OwnedDirectories,
    claim: RunClaim,
    preset_name: Literal["codex", "claude"],
) -> Path:
    resources = await store.session_resources(claim.run.session_id)
    if not resources:
        return await provision_directory(
            store,
            directories,
            token=claim.token,
            session_id=claim.run.session_id,
            purpose="claude_home" if preset_name == "claude" else "codex_home",
        )
    if len(resources) == 1 and not (directories.path / resources[0].name).exists():
        await directories.create(resources[0])
    return await session_directory(store, directories, claim.run.session_id, preset_name)


class NativeExecution:
    def __init__(
        self,
        claim: RunClaim,
        store: ReviewStore,
        directories: OwnedDirectories,
        sources: GitSourceReader,
        command: tuple[str, ...],
        recovery: ProcessRecovery,
        processes: ProcessStore,
        is_current: Callable[[], Awaitable[bool]],
        preset_name: Literal["codex", "claude"] = "codex",
        workspaces: SnapshotWorkspaces | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.progress = progress
        self.preset_name: Literal["codex", "claude"] = preset_name
        self.workspaces = workspaces or SnapshotWorkspaces(directories)
        self.claim = claim
        self.store = store
        self.directories = directories
        self.sources = sources
        self.command = command
        self.recovery = recovery
        self.processes = processes
        self.is_current = is_current
        self.execution = CapturedProcess()

    async def run(
        self,
        context: ReviewContext,
        backend_session_id: str | None,
        checkpoint: Callable[[str], Awaitable[None]],
    ) -> ReviewOutput:
        root = await ensure_session_directory(
            self.store, self.directories, self.claim, self.preset_name
        )

        def initialize_home() -> None:
            for name in ("agent-home", "runs"):
                directory = root / name
                if directory.is_symlink():
                    raise NativeError("Managed native directory was replaced by a symlink")
                directory.mkdir(mode=0o700, exist_ok=True)
            # Share only the native login, not user MCP/plugins/rules/configuration.
            if self.preset_name == "claude":
                auth = (
                    Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
                    / ".credentials.json"
                )
            else:
                auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
            target = root / "agent-home" / auth.name
            if auth.is_file() and not target.exists() and not target.is_symlink():
                target.symlink_to(auth.absolute())

        await settled_io(initialize_home)
        resource = (await self.store.session_resources(self.claim.run.session_id))[0]
        snapshot_root = await self.workspaces.session_path(resource)
        workdir = snapshot_root / "runs" / str(context.run_id)
        await materialize_checkout(self.sources, workdir, progress=self.progress)
        prompt = render_git_prompt(context)

        await save_json(root / "current.json", {"run_id": str(context.run_id)})
        env = background_environment(root / "agent-home", self.preset_name)
        preset = native_preset(self.preset_name)
        if not await self.is_current():
            raise NativeError("Review became stale during source preparation")
        await self.recovery.prepare_launch(self.claim.token, "background")
        if self.progress is not None:
            self.progress("Running AI review…")

        async def native_checkpoint(identity: str) -> None:
            try:
                UUID(identity)
            except ValueError:
                raise NativeError("Invalid native conversation identity") from None
            await checkpoint(identity)

        capture = asyncio.create_task(
            self.execution.capture(
                preset.argv(
                    preset.command_at(self.command, workdir),
                    backend_session_id,
                    session_name=f"diffbutler-pr-{context.pr.ref.number}",
                ),
                cwd=workdir,
                env=env,
                input=prompt.content,
                checkpoint=native_checkpoint,
                output_format=preset.output_format,
            )
        )
        try:
            # capture starts the spawn task synchronously on its first scheduling turn.
            await asyncio.sleep(0)
            if self.execution.spawn is not None:
                process = await asyncio.shield(self.execution.spawn)
                await self.processes.bind_launcher(self.claim.token, process.pid)
            return await capture
        finally:
            if not capture.done():
                capture.cancel()
                await asyncio.gather(capture, return_exceptions=True)

    async def stop_and_wait(self) -> None:
        await self.execution.stop_and_wait()


class NativeSessionCleaner:
    """Remove owned snapshots and native homes, then release shared object pins."""

    def __init__(
        self,
        directories: OwnedDirectories,
        repositories: RepositoryCache | None = None,
        workspaces: SnapshotWorkspaces | None = None,
    ) -> None:
        self.directories = directories
        self.repositories = repositories
        self.workspaces = workspaces or SnapshotWorkspaces(directories)

    async def cleanup(self, session: CleanupSession) -> None:
        resources = tuple(OwnedDirectory.from_manifest(dict(item)) for item in session.resources)
        for resource in resources:
            if resource.session_id != session.id or resource.root_id != self.directories.root_id:
                raise ValueError("Cleanup manifest ownership mismatch")
        if session.transcript_ref is not None:
            raise ValueError("External transcript deletion is unsupported")
        if session.backend_session_id is not None and (
            len(resources) != 1 or resources[0].purpose not in {"codex_home", "claude_home"}
        ):
            raise NativeError("Native cleanup requires an owned native home manifest")
        # Deleting the entire dedicated native home removes local conversation state,
        # caches, and indexes without deriving paths from the opaque backend ID.
        for resource in resources:
            await self.workspaces.cleanup(resource)
        await OwnedSessionCleaner(self.directories, {}).cleanup(
            replace(session, backend_session_id=None)
        )
        if self.repositories is not None:
            await self.repositories.release(session.id)


class SessionLaunch(BaseModel):
    model_config = ConfigDict(frozen=True)
    token: UUID
    session_id: UUID
    argv: tuple[str, ...]
    cwd: Path
    agent_home: Path
    preset_name: Literal["codex", "claude"] = "codex"


async def prepare_native_handoff(
    store: ReviewStore,
    lifecycle: LifecycleStore,
    directories: OwnedDirectories,
    session_id: UUID,
    command: tuple[str, ...],
    preset_name: Literal["codex", "claude"] = "codex",
    *,
    pr_number: int,
    workspaces: SnapshotWorkspaces | None = None,
) -> SessionLaunch:
    lease = await lifecycle.acquire_foreground(session_id)
    try:
        root = await session_directory(store, directories, session_id, preset_name)
        run_id = UUID(json.loads((root / "current.json").read_text())["run_id"])
        run = await store.get_run(run_id)
        if run.session_id != session_id:
            raise NativeError("Pinned review workspace identity mismatch")
        resource = (await store.session_resources(session_id))[0]
        snapshots = workspaces or SnapshotWorkspaces(directories)
        source_record = root / f"source-{run_id}.json"
        # Sessions created before shared storage keep their original workspace.
        snapshot_root = await snapshots.session_path(resource) if source_record.is_file() else root
        cwd = snapshot_root / "runs" / str(run_id)
        if cwd.is_symlink():
            raise NativeError("Pinned review workspace is unavailable")
        if not cwd.exists():
            if not source_record.is_file():
                raise NativeError("Pinned review workspace is unavailable")
            data = json.loads(await settled_io(source_record.read_text))
            reader = await GitSourceReader.open(
                Path(data["repository"]), base_sha=run.base_sha, head_sha=run.head_sha
            )
            prepare_sources = materialize_checkout if data.get("format") == "git" else materialize
            await prepare_sources(reader, cwd)
        if not cwd.is_dir():
            raise NativeError("Pinned review workspace is unavailable")
        preset = native_preset(preset_name)
        return SessionLaunch(
            token=lease.token,
            session_id=session_id,
            argv=preset.argv(
                preset.command_at(command, cwd),
                lease.backend_session_id,
                foreground=True,
                session_name=f"diffbutler-pr-{pr_number}",
            ),
            cwd=cwd,
            agent_home=root / "agent-home",
            preset_name=preset_name,
        )
    except BaseException:
        await lifecycle.release_foreground(lease.token)
        raise


def foreground_environment(launch: SessionLaunch) -> Mapping[str, str]:
    home_key = "CLAUDE_CONFIG_DIR" if launch.preset_name == "claude" else "CODEX_HOME"
    return {**os.environ, home_key: str(launch.agent_home)}
