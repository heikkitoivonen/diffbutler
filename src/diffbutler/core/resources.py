"""Persist resource ownership before filesystem preparation, under a worker claim."""

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

from diffbutler.domain.resources import OwnedDirectory
from diffbutler.reviewers.resources import settled_io
from diffbutler.store.base import ReviewStore


async def save_json(path: Path, data: dict[str, str]) -> None:
    """Publish a complete ownership/source record before creating dependent resources."""

    def write() -> None:
        temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
        with temporary.open("x") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    await settled_io(write)


class DirectoryProvisioner(Protocol):
    def reserve(self, session_id: UUID) -> OwnedDirectory: ...
    async def create(self, resource: OwnedDirectory) -> Path: ...


async def provision_directory(
    store: ReviewStore,
    directories: DirectoryProvisioner,
    *,
    token: UUID,
    session_id: UUID,
    purpose: Literal["artifact", "codex_home", "claude_home"] = "artifact",
) -> Path:
    """Failures retain the manifest; ordinary completion can clean absent resources."""
    resource = replace(directories.reserve(session_id), purpose=purpose)
    await store.register_resource(token, resource)
    return await directories.create(resource)
