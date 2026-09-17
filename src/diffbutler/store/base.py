"""Persistence seam for account-scoped atomic queue updates."""

from typing import Protocol
from uuid import UUID

from diffbutler.domain.forge import Account, PRRef
from diffbutler.domain.review import (
    AnalysisIdentity,
    ReviewPolicy,
    RunClaim,
    RunRecord,
    SessionRecord,
)
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


class ReviewStore(Protocol):
    async def get_session(self, session_id: UUID) -> SessionRecord: ...
    async def bind_backend_session(self, token: UUID, backend_session_id: str) -> SessionRecord: ...
    async def suspend_session(self, session_id: UUID) -> SessionRecord: ...
    async def get_run(self, run_id: UUID) -> RunRecord: ...
    async def request_review(
        self, identity: AnalysisIdentity, policy: ReviewPolicy
    ) -> RunRecord: ...
    async def claim_review(
        self, run_id: UUID, *, capacity: int, policy: ReviewPolicy
    ) -> RunClaim | None: ...
    async def cancel_review(self, run_id: UUID) -> RunRecord: ...
    async def release_review(self, token: UUID) -> RunRecord: ...
