"""Private owned directories and idempotent cleanup; never infer backend paths."""

import asyncio
import os
import shutil
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from diffbutler.domain.lifecycle import CleanupSession
from diffbutler.domain.resources import OwnedDirectory


async def settled_io[T](operation: Callable[[], T]) -> T:
    # Cancelling to_thread does not stop its thread. Keep ownership until I/O ends.
    task = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result


class OwnedDirectories:
    """A trusted, private root, exclusively administered by DiffButler.

    Parent directories are trusted configuration, never PR-controlled paths. Do
    not mount other filesystems beneath this root or expose it to reviewers.
    All destructive traversal uses descriptor-relative symlink-safe rmtree.
    """

    def __init__(self, path: Path, root_id: UUID) -> None:
        self.path = path
        self.root_id = root_id

    @classmethod
    async def initialize(cls, path: Path) -> OwnedDirectories:
        def initialize() -> OwnedDirectories:
            root_id = uuid4()
            # Existing directories are never adopted, including repository homes.
            path.mkdir(mode=0o700)
            marker = path / ".diffbutler-root"
            with marker.open("x", encoding="ascii") as stream:
                os.chmod(marker, 0o600)
                stream.write(str(root_id))
                stream.flush()
                os.fsync(stream.fileno())
            return cls(path, root_id)

        return await settled_io(initialize)

    @classmethod
    async def reopen(cls, path: Path) -> OwnedDirectories:
        def read() -> OwnedDirectories:
            root_id = UUID((path / ".diffbutler-root").read_text(encoding="ascii"))
            directories = cls(path, root_id)
            fd = directories._open()
            os.close(fd)
            return directories

        return await settled_io(read)

    def _open(self) -> int:
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("Safe descriptor-relative cleanup is unavailable")
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("Managed root must be private and owned by this user")
            marker = os.open(".diffbutler-root", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(marker, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("Invalid managed root marker")
                if stream.read(100) != str(self.root_id).encode("ascii"):
                    raise ValueError("Managed root identity mismatch")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def reserve(self, session_id: UUID) -> OwnedDirectory:
        return OwnedDirectory(self.root_id, session_id, uuid4())

    def _validate(self, resource: OwnedDirectory) -> None:
        if resource.root_id != self.root_id:
            raise ValueError("Resource belongs to a different managed root")

    async def create(self, resource: OwnedDirectory) -> Path:
        """Call only after committing its manifest while holding session ownership."""
        self._validate(resource)

        def create() -> Path:
            fd = self._open()
            try:
                # Exclusive creation: never adopt a preexisting directory.
                os.mkdir(resource.name, mode=0o700, dir_fd=fd)
                os.fsync(fd)
                return self.path / resource.name
            finally:
                os.close(fd)

        return await settled_io(create)

    async def delete(self, resource: OwnedDirectory) -> None:
        self._validate(resource)

        def delete() -> None:
            fd = self._open()
            try:
                # A reserved but never created resource, or an earlier retry.
                with suppress(FileNotFoundError):
                    shutil.rmtree(resource.name, dir_fd=fd)
                os.fsync(fd)
            finally:
                os.close(fd)

        await settled_io(delete)


class BackendSessionDeleter(Protocol):
    async def delete_session(self, backend_session_id: str) -> None:
        """Supported, idempotent deletion; settle all operations before returning."""
        ...


class OwnedSessionCleaner:
    def __init__(
        self,
        directories: OwnedDirectories,
        backends: Mapping[str, BackendSessionDeleter],
    ) -> None:
        self.directories = directories
        self.backends = dict(backends)

    async def cleanup(self, session: CleanupSession) -> None:
        resources = tuple(OwnedDirectory.from_manifest(dict(item)) for item in session.resources)
        # Validate everything before beginning any destructive work.
        for resource in resources:
            if resource.session_id != session.id or resource.root_id != self.directories.root_id:
                raise ValueError("Cleanup manifest ownership mismatch")
        if session.transcript_ref is not None:
            raise ValueError("External transcript deletion is unsupported")
        backend = self.backends.get(session.reviewer_id)
        if session.backend_session_id is not None:
            if backend is None:
                raise ValueError("Backend session deletion is unsupported")
            await backend.delete_session(session.backend_session_id)
        for resource in resources:
            await self.directories.delete(resource)
