"""Persist resource ownership before filesystem preparation, under a worker claim."""

from dataclasses import replace
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from diffbutler.domain.resources import OwnedDirectory
from diffbutler.store.base import ReviewStore


class DirectoryProvisioner(Protocol):
    def reserve(self, session_id: UUID) -> OwnedDirectory: ...
    async def create(self, resource: OwnedDirectory) -> Path: ...


async def provision_directory(
    store: ReviewStore,
    directories: DirectoryProvisioner,
    *,
    token: UUID,
    session_id: UUID,
    purpose: Literal["artifact", "codex_home"] = "artifact",
) -> Path:
    """Failures retain the manifest; ordinary completion can clean absent resources."""
    resource = replace(directories.reserve(session_id), purpose=purpose)
    await store.register_resource(token, resource)
    return await directories.create(resource)
