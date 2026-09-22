"""Reconstructible snapshots; persistent native homes retain their identities."""

import json
from pathlib import Path
from uuid import UUID, uuid4

from diffbutler.core.repositories import file_lock
from diffbutler.core.resources import save_json
from diffbutler.core.snapshot import write_snapshot
from diffbutler.domain.resources import OwnedDirectory
from diffbutler.reviewers.resources import OwnedDirectories, settled_io
from diffbutler.sources.git import GitSourceReader


class SnapshotWorkspaces:
    def __init__(self, homes: OwnedDirectories, path: Path | None = None) -> None:
        self.homes = homes
        self.path = path

    async def directories(self, *, create: bool) -> OwnedDirectories | None:
        record = self.homes.path / ".snapshot-location.json"
        async with file_lock(self.homes.path / ".snapshot-lock"):
            if record.exists():
                data = json.loads(await settled_io(record.read_text))
                path = Path(data["path"])
                identity = UUID(data["root_id"])
                if not path.is_absolute():
                    raise ValueError("Invalid snapshot root")
                if self.path is not None and self.path.absolute() != path:
                    raise ValueError("Snapshot root cannot change while using this managed home")
            elif self.path is None or not create:
                return None
            else:
                path = self.path.absolute()
                identity = uuid4()
                # Do not adopt preexisting directories, even if they have a marker.
                if path.exists() or path.is_symlink():
                    raise ValueError("Snapshot root must be a new private directory")

                await save_json(record, {"path": str(path), "root_id": str(identity)})
            if not path.exists() and not path.is_symlink():
                if not create:
                    return None
                await settled_io(lambda: path.parent.mkdir(parents=True, exist_ok=True))
                return await OwnedDirectories.initialize(path, root_id=identity)
            directories = await OwnedDirectories.reopen(path)
            if directories.root_id != identity:
                raise ValueError("Snapshot root identity mismatch")
            return directories

    @staticmethod
    def resource(directories: OwnedDirectories, home: OwnedDirectory) -> OwnedDirectory:
        return OwnedDirectory(directories.root_id, home.session_id, home.resource_id)

    async def session_path(self, home: OwnedDirectory) -> Path:
        directories = await self.directories(create=True)
        if directories is None:
            return self.homes.path / home.name
        resource = self.resource(directories, home)
        path = directories.path / resource.name
        if path.is_symlink():
            raise ValueError("Snapshot workspace was replaced by a symlink")
        if not path.exists():
            await directories.create(resource)
        return path

    async def cleanup(self, home: OwnedDirectory) -> None:
        directories = await self.directories(create=False)
        if directories is not None:
            await directories.delete(self.resource(directories, home))


async def materialize(reader: GitSourceReader, destination: Path) -> None:
    """Publish only complete snapshots; an interrupted preparation is reconstructible."""
    temporary = destination.with_name(f".{destination.name}-{uuid4().hex}.tmp")
    await settled_io(lambda: destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True))
    await write_snapshot(reader, temporary)
    await settled_io(lambda: temporary.rename(destination))
