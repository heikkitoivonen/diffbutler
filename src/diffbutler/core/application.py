"""Configured shared-core entry points with owned database/forge lifetimes."""

import asyncio
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from diffbutler.config.settings import Settings, load_settings
from diffbutler.core.pr_lookup import ReviewTarget, numbered_prs, open_pr
from diffbutler.core.sync import DiffButler, PullRequestRow, SyncReport, list_queue
from diffbutler.domain.forge import Account, PRPage, PRQuery, PRRef
from diffbutler.forges.base import ForgeError
from diffbutler.forges.registry import open_forge
from diffbutler.store.dashboard import SQLiteDashboardStore
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
        preferences = await SQLiteDashboardStore(store.database).preferences()
        yield DiffButler(
            store,
            forge,
            expected_login=config.login,
            monitor_repositories=tuple(config.repositories)
            if preferences.get("monitor_all_prs") == "true"
            else (),
        )
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
        from diffbutler.core.managed import ManagedReviews, owned_directories

        managed = ManagedReviews(store.database, settings, await owned_directories(settings))
        await managed.cleanup()
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


async def review_targets_configured(path: Path | None, value: str) -> tuple[ReviewTarget, ...]:
    """Resolve without following PRs or requesting any AI work."""
    value = value.strip()
    numeric = re.fullmatch(r"#?([1-9][0-9]*)", value)
    if not numeric and not value.startswith("https://"):
        raise ForgeError("Enter a PR link or a positive PR number")
    found: dict[tuple[Account, PRRef], ReviewTarget] = {}
    async with local_store(path) as (store, settings):
        if not settings.forges:
            raise ForgeError("No forge configured")
        if numeric and not any(config.repositories for config in settings.forges):
            raise ForgeError("No monitored repositories configured; enter a PR link instead")
        matched_url = False
        for index, config in enumerate(settings.forges, 1):
            if numeric and not config.repositories:
                continue
            async with remote_service(store, settings, index) as service:
                if numeric:
                    prs = await numbered_prs(service.forge, config.repositories, int(numeric[1]))
                else:
                    try:
                        ref = service.forge.ref(value)
                    except ForgeError:
                        continue
                    matched_url = True
                    pr = await open_pr(service.forge, ref)
                    prs = (pr,) if pr else ()
                account = await service.forge.whoami()
                if config.login and account.login.casefold() != config.login.casefold():
                    raise ForgeError("Authenticated identity does not match configured account")
                for pr in prs:
                    found.setdefault((account, pr.ref), ReviewTarget(index, account, pr))
        if not numeric and not matched_url:
            raise ForgeError("Enter a valid PR link on a configured forge")
    return tuple(found.values())


async def follow_review_target_configured(
    path: Path | None, target: ReviewTarget
) -> PullRequestRow:
    """Revalidate the chosen account and open state before tracking a review target."""
    async with (
        local_store(path) as (store, settings),
        remote_service(store, settings, target.forge_index) as service,
    ):
        if await service.forge.whoami() != target.account:
            raise ForgeError("Configured account changed; look up the PR again")
        return await service.follow_pr(target.pull_request.ref, for_review=True)


async def local_queue(
    path: Path | None,
    view: Literal["active", "requested", "following"],
    login: str | None,
) -> tuple[PullRequestRow, ...]:
    async with local_store(path) as (store, settings):
        return await list_queue(
            store,
            view=view,
            login=login,
            include_own_prs=settings.ui.include_own_prs,
            include_assigned_prs=settings.ui.include_assigned_prs,
        )
