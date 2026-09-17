"""Persist resource ownership before filesystem preparation, under a worker claim."""

from pathlib import Path
from typing import Protocol
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
) -> Path:
    """Failures retain the manifest; ordinary completion can clean absent resources."""
    resource = directories.reserve(session_id)
    await store.register_resource(token, resource)
    return await directories.create(resource)
