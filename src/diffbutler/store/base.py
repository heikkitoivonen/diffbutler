"""Persistence seam for account-scoped atomic queue updates."""

from typing import Protocol

from diffbutler.domain.forge import Account, PRRef
from diffbutler.domain.sync import QueueSnapshot, TrackedPR


class SyncConflict(Exception):
    """Another process committed after the sync snapshot was read; refresh first."""


class QueueStore(Protocol):
    async def snapshot(self, account: Account) -> QueueSnapshot: ...
    async def apply(
        self,
        account: Account,
        version: int,
        items: tuple[TrackedPR, ...],
        *,
        refreshed: frozenset[PRRef] = frozenset(),
    ) -> None: ...
    async def accounts(self) -> tuple[Account, ...]: ...
