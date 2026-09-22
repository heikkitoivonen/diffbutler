"""Persistent, owned Git object storage with cross-process serialization and pins."""

import asyncio
import fcntl
import json
import os
import stat
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from diffbutler.core.resources import save_json
from diffbutler.domain.forge import PRRef
from diffbutler.domain.resources import OwnedDirectory
from diffbutler.reviewers.resources import OwnedDirectories, settled_io
from diffbutler.sources.fetch import fetch_repository, git_fetch_command
from diffbutler.sources.git import GitSourceReader, SourceError


@asynccontextmanager
async def file_lock(path: Path) -> AsyncGenerator[None]:
    """Cancellation never leaves a background thread waiting on an advisory lock."""
    await settled_io(lambda: path.parent.mkdir(parents=True, exist_ok=True))
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Invalid managed storage lock")
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        os.close(fd)


async def open_directories(path: Path) -> OwnedDirectories:
    marker = path / ".diffbutler-repositories"
    if path.exists() or path.is_symlink():
        directories = await OwnedDirectories.reopen(path)
        if marker.is_symlink() or await settled_io(marker.read_text) != "1\n":
            raise ValueError("Invalid shared repository root")
        return directories
    await settled_io(lambda: path.parent.mkdir(parents=True, exist_ok=True))
    directories = await OwnedDirectories.initialize(path)
    await settled_io(lambda: marker.write_text("1\n"))
    return directories


class RepositoryCache:
    def __init__(self, path: Path, retention_days: int = 30) -> None:
        self.path = path
        self.retention_seconds = retention_days * 86400

    @property
    def lock_path(self) -> Path:
        # Outside the root so initialization, deletion, and replacement share a lock.
        return self.path.parent / f".{self.path.name}.lock"

    @staticmethod
    def resource(directories: OwnedDirectories, ref: PRRef) -> OwnedDirectory:
        identity = json.dumps([ref.forge, ref.namespace, ref.repository])
        key = uuid5(NAMESPACE_URL, identity)
        return OwnedDirectory(directories.root_id, key, key)

    async def existing(self, ref: PRRef) -> Path | None:
        if not self.path.exists():
            return None
        directories = await open_directories(self.path)
        path = self.path / self.resource(directories, ref).name
        return path if path.is_dir() and not path.is_symlink() else None

    async def prepare(
        self,
        ref: PRRef,
        session_id: UUID,
        run_id: UUID,
        base_sha: str,
        head_sha: str,
        *,
        local_path: Path | None = None,
        transport: Literal["auto", "https", "ssh"] = "auto",
    ) -> tuple[Path, Literal["https", "ssh"] | None]:
        async with file_lock(self.lock_path):
            directories = await open_directories(self.path)
            resource = self.resource(directories, ref)
            path = self.path / resource.name
            if not path.exists():
                await directories.create(resource)
            if path.is_symlink():
                raise SourceError("Managed repository was replaced by a symlink")
            # Register before any fetch, including failed/interrupted preparations.
            pin = path / "pins" / f"{session_id.hex}-{run_id.hex}.json"

            def register() -> None:
                pin.parent.mkdir(mode=0o700, exist_ok=True)
                (path / "last-used").touch()

            await settled_io(register)
            await save_json(pin, {"base": base_sha, "head": head_sha})
            learned: Literal["https", "ssh"] | None = None
            try:
                await GitSourceReader.open(path, base_sha=base_sha, head_sha=head_sha)
            except SourceError:
                imported = False
                if local_path is not None:
                    try:
                        await GitSourceReader.open(local_path, base_sha=base_sha, head_sha=head_sha)
                    except SourceError:
                        pass
                    else:
                        await git_fetch_command(path, "init", "--bare", "--template=")
                        await git_fetch_command(
                            path,
                            "fetch",
                            "--depth=1",
                            "--no-tags",
                            "--no-recurse-submodules",
                            "--",
                            str(local_path.absolute()),
                            base_sha,
                            head_sha,
                            local=True,
                        )
                        imported = True
                if not imported:
                    learned = await fetch_repository(
                        path, ref, base_sha, head_sha, transport=transport
                    )
            await GitSourceReader.open(path, base_sha=base_sha, head_sha=head_sha)
            for side, sha in (("base", base_sha), ("head", head_sha)):
                await git_fetch_command(
                    path, "update-ref", f"refs/diffbutler/{session_id.hex}/{run_id.hex}/{side}", sha
                )
            return path, learned

    async def release(self, session_id: UUID) -> None:
        if not self.path.exists():
            return
        async with file_lock(self.lock_path):
            directories = await open_directories(self.path)
            for resource in await self._resources(directories):
                path = self.path / resource.name
                pins = await settled_io(
                    lambda path=path: list((path / "pins").glob(f"{session_id.hex}-*.json"))
                )
                for pin in pins:
                    run_id = UUID(pin.stem.split("-")[1])
                    if (path / "HEAD").exists():
                        for side in ("base", "head"):
                            await git_fetch_command(
                                path,
                                "update-ref",
                                "-d",
                                f"refs/diffbutler/{session_id.hex}/{run_id.hex}/{side}",
                            )
                    await settled_io(pin.unlink)
                    await settled_io(lambda path=path: (path / "last-used").touch())

    async def prune(self) -> None:
        """Evict whole inactive repositories; never expire a live session's pins."""
        if not self.path.exists():
            return
        async with file_lock(self.lock_path):
            directories = await open_directories(self.path)
            for resource in await self._resources(directories):
                path = self.path / resource.name

                def expired(path: Path = path) -> bool:
                    if any((path / "pins").glob("*.json")):
                        return False
                    marker = path / "last-used"
                    modified = marker.stat().st_mtime if marker.exists() else path.stat().st_mtime
                    return time.time() - modified >= self.retention_seconds

                if await settled_io(expired):
                    await directories.delete(resource)

    async def _resources(self, directories: OwnedDirectories) -> list[OwnedDirectory]:
        def read() -> list[OwnedDirectory]:
            result: list[OwnedDirectory] = []
            for path in directories.path.iterdir():
                if path.name in {".diffbutler-root", ".diffbutler-repositories"}:
                    continue
                try:
                    left, right = path.name.split("-")
                    resource = OwnedDirectory(directories.root_id, UUID(left), UUID(right))
                except ValueError:
                    raise ValueError("Unrecognized managed repository resource") from None
                if (
                    resource.name != path.name
                    or resource.session_id != resource.resource_id
                    or path.is_symlink()
                    or not path.is_dir()
                ):
                    raise ValueError("Invalid managed repository resource")
                result.append(resource)
            return result

        return await settled_io(read)
