"""Configured shared-core entry points with owned database/forge lifetimes."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from diffbutler.config.settings import Settings, load_settings
from diffbutler.core.sync import DiffButler, PullRequestRow, SyncReport, list_queue
from diffbutler.domain.forge import PRPage, PRQuery
from diffbutler.forges.base import ForgeError
from diffbutler.forges.registry import open_forge
from diffbutler.store.database import Database
from diffbutler.store.queue import SQLiteQueueStore


@asynccontextmanager
async def local_store(path: Path | None) -> AsyncGenerator[tuple[SQLiteQueueStore, Settings]]:
    settings = await asyncio.to_thread(load_settings, path)
    database = Database(settings.db.url)
    try:
        await database.initialize(auto_migrate=settings.db.auto_migrate)
        yield SQLiteQueueStore(database), settings
    finally:
        await database.close()


@asynccontextmanager
async def remote_service(
    store: SQLiteQueueStore,
    settings: Settings,
    index: int,
) -> AsyncGenerator[DiffButler]:
    if not 1 <= index <= len(settings.forges):
        raise ForgeError("Select a configured forge with --forge (one-based index)")
    config = settings.forges[index - 1]
    forge = await open_forge(config)
    try:
        yield DiffButler(store, forge, expected_login=config.login)
    finally:
        await forge.close()


async def sync_configured(path: Path | None, index: int | None = None) -> tuple[SyncReport, ...]:
    async with local_store(path) as (store, settings):
        indexes = [index] if index is not None else list(range(1, len(settings.forges) + 1))
        if not indexes:
            raise ForgeError("No forge configured; add a [[forges]] section to configuration")
        reports: list[SyncReport] = []
        for selected in indexes:
            async with remote_service(store, settings, selected) as service:
                reports.append(await service.sync())
        return tuple(reports)


async def follow_configured(path: Path | None, index: int, value: str) -> PullRequestRow:
    async with (
        local_store(path) as (store, settings),
        remote_service(store, settings, index) as service,
    ):
        return await service.follow_pr(service.forge.ref(value))


async def browse_configured(
    path: Path | None,
    index: int,
    query: PRQuery,
    cursor: str | None,
) -> PRPage:
    async with (
        local_store(path) as (store, settings),
        remote_service(store, settings, index) as service,
    ):
        return await service.browse_prs(query, cursor)


async def local_queue(
    path: Path | None,
    view: Literal["active", "requested", "following"],
    login: str | None,
) -> tuple[PullRequestRow, ...]:
    async with local_store(path) as (store, _settings):
        return await list_queue(store, view=view, login=login)
