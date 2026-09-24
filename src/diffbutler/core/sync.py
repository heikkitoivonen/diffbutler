"""Shared async discovery, follow-up, and browsing services; no reviewer capability."""

from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, ConfigDict

from diffbutler.domain.forge import Account, Discovery, PRPage, PRQuery, PRRef
from diffbutler.domain.sync import ReviewActivity, TrackedPR, reconcile
from diffbutler.forges.base import ForgeError, ReadForge
from diffbutler.store.base import QueueStore, SyncConflict


class PullRequestRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    account: Account
    tracked: TrackedPR
    warnings: tuple[str, ...] = ()


class SyncReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    account: Account
    discovered: int
    refreshed: int
    active: int
    complete: bool
    warnings: tuple[str, ...]


class DiffButler:
    def __init__(
        self,
        store: QueueStore,
        forge: ReadForge,
        *,
        expected_login: str | None = None,
        monitor_repositories: tuple[str, ...] = (),
    ):
        self.store = store
        self.forge = forge
        self.expected_login = expected_login
        self.monitor_repositories = monitor_repositories

    async def _account(self) -> Account:
        account = await self.forge.whoami()
        if account.forge != self.forge.id or (
            self.expected_login is not None and account.login.lower() != self.expected_login.lower()
        ):
            raise ForgeError("Authenticated identity does not match configured account")
        return account

    async def sync(self) -> SyncReport:
        account = await self._account()
        stale_results: dict[PRRef, TrackedPR] = {}
        for attempt in range(3):
            snapshot = await self.store.snapshot(account)
            try:
                discovery = await self.forge.list_review_requests()
            except ForgeError as exc:
                discovery = Discovery((), False, (f"Discovery failed: {exc}",))
            warnings = list(discovery.warnings)
            complete = discovery.complete
            discovered = {item.pull_request.ref: item.pull_request for item in discovery.items}
            sources: dict[PRRef, set[str]] = {}
            for item in discovery.items:
                sources.setdefault(item.pull_request.ref, set()).update(item.sources)
            # Broad monitoring is independent of review-request membership.
            for repository in self.monitor_repositories:
                cursor: str | None = None
                seen: set[str] = set()
                try:
                    while True:
                        page = await self.forge.search_pull_requests(
                            PRQuery(repository=repository, page_size=100), cursor
                        )
                        complete = complete and page.complete
                        warnings.extend(page.warnings)
                        for pr in page.items:
                            if (
                                pr.state == "open"
                                and pr.ref.forge == account.forge
                                and f"{pr.ref.namespace}/{pr.ref.repository}".lower()
                                == repository.lower()
                            ):
                                discovered[pr.ref] = pr
                        cursor = page.next_cursor
                        if cursor is None:
                            break
                        if cursor in seen:
                            raise ForgeError("Repeated pagination cursor")
                        seen.add(cursor)
                except ForgeError as exc:
                    complete = False
                    warnings.append(f"Open-PR monitoring is incomplete: {exc}")
            previous = {item.pull_request.ref: item for item in snapshot.items}
            refs = set(discovered) | {
                ref
                for ref, item in previous.items()
                if item.following and item.pull_request.state == "open"
            }
            updated = dict(previous)
            refreshed: set[PRRef] = set()
            for ref in sorted(
                refs, key=lambda ref: (ref.forge, ref.namespace, ref.repository, ref.number)
            ):
                old = previous.get(ref)
                stale = stale_results.get(ref)
                if old and stale and old.manual_after != stale.manual_after:
                    # Consume evidence fetched before a newer manual choice, even
                    # if the forge's clock is ahead of this host's clock.
                    old = replace(old, manual_after=max(old.manual_after, stale.review_key))
                membership = set(sources.get(ref, ()))
                if not discovery.complete and old:
                    membership.update(old.sources)
                try:
                    # Followed PRs refresh by identity even when search is empty or stale.
                    pr = await self.forge.get_pull_request(ref) if old else discovered[ref]
                    if pr.ref != ref:
                        raise ForgeError("Forge returned a different PR identity")
                except ForgeError as exc:
                    complete = False
                    warnings.append(
                        f"Could not refresh {ref.namespace}/{ref.repository}#{ref.number}: {exc}"
                    )
                    if old:
                        updated[ref] = replace(
                            old, sources=tuple(sorted(membership)), refresh_pending=True
                        )
                    continue
                try:
                    activity = await self.forge.get_review_activity(ref)
                    if activity.account != account:
                        raise ForgeError("Account changed during activity refresh")
                except ForgeError as exc:
                    activity = ReviewActivity(account, complete=False, warnings=(str(exc),))
                complete = complete and activity.complete
                warnings.extend(activity.warnings)
                updated[ref] = reconcile(old, pr, activity, tuple(sorted(membership)))
                refreshed.add(ref)
            # Clear stale membership only after complete discovery, including closed monitors.
            if discovery.complete:
                for ref, item in updated.items():
                    if ref not in refs:
                        updated[ref] = replace(item, sources=tuple(sorted(sources.get(ref, ()))))
            if await self._account() != account:
                raise ForgeError("Account changed during sync; no changes saved")
            try:
                await self.store.apply(
                    account,
                    snapshot.version,
                    tuple(updated.values()),
                    refreshed=frozenset(refreshed),
                )
            except SyncConflict:
                stale_results = updated
                if attempt == 2:
                    raise ForgeError("Queue changed concurrently; retry sync") from None
                continue
            return SyncReport(
                account=account,
                discovered=len(discovered),
                refreshed=len(refreshed),
                active=sum(item.state != "DONE" for item in updated.values()),
                complete=complete,
                warnings=tuple(dict.fromkeys(warnings)),
            )
        raise AssertionError("unreachable")

    async def follow_pr(self, ref: PRRef) -> PullRequestRow:
        account = await self._account()
        stale: TrackedPR | None = None
        for attempt in range(3):
            snapshot = await self.store.snapshot(account)
            previous = next((item for item in snapshot.items if item.pull_request.ref == ref), None)
            if previous and stale and previous.manual_after != stale.manual_after:
                previous = replace(
                    previous, manual_after=max(previous.manual_after, stale.review_key)
                )
            pr = await self.forge.get_pull_request(ref)
            if pr.ref != ref:
                raise ForgeError("Forge returned a different PR identity")
            # Following succeeds even if activity access fails; return the warning to the UI.
            try:
                activity = await self.forge.get_review_activity(ref)
            except ForgeError as exc:
                activity = ReviewActivity(account, complete=False, warnings=(str(exc),))
            if activity.account != account or await self._account() != account:
                raise ForgeError("Account changed during follow; no changes saved")
            item = reconcile(previous, pr, activity, previous.sources if previous else ())
            item = replace(item, following=True)
            try:
                await self.store.apply(
                    account, snapshot.version, (item,), refreshed=frozenset({ref})
                )
            except SyncConflict:
                stale = item
                if attempt == 2:
                    raise ForgeError("Queue changed concurrently; retry follow") from None
                continue
            persisted = await self.store.snapshot(account)
            result = next(row for row in persisted.items if row.pull_request.ref == ref)
            return PullRequestRow(account=account, tracked=result, warnings=activity.warnings)
        raise AssertionError("unreachable")

    async def browse_prs(self, query: PRQuery, cursor: str | None = None) -> PRPage:
        await self._account()
        return await self.forge.search_pull_requests(query, cursor)


async def list_queue(
    store: QueueStore,
    *,
    view: Literal["active", "requested", "following"] = "active",
    login: str | None = None,
    include_own_prs: bool = False,
) -> tuple[PullRequestRow, ...]:
    """Read local accounts without resolving credentials or making network requests."""
    rows: list[PullRequestRow] = []
    for account in await store.accounts():
        if login is not None and account.login.lower() != login.lower():
            continue
        for item in (await store.snapshot(account)).items:
            if (
                not include_own_prs
                and item.pull_request.author.casefold() == account.login.casefold()
            ):
                continue
            include = (
                item.state != "DONE"
                if view == "active"
                else any(
                    source == f"user:{account.login.lower()}" or source.startswith("team:")
                    for source in item.sources
                )
                if view == "requested"
                else item.following
            )
            if include:
                rows.append(PullRequestRow(account=account, tracked=item))
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.account.forge,
                row.account.login,
                row.tracked.pull_request.ref.namespace,
                row.tracked.pull_request.ref.repository,
                row.tracked.pull_request.ref.number,
            ),
        )
    )
