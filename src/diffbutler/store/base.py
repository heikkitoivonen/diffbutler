"""Persistence seam for account-scoped atomic queue updates."""

from typing import Protocol
from uuid import UUID

from diffbutler.domain.forge import Account, PRRef
from diffbutler.domain.lifecycle import CleanupClaim, ForegroundLease, HeldOperation
from diffbutler.domain.review import (
    AnalysisIdentity,
    ReviewPolicy,
    ReviewResultRecord,
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
    async def get_result(self, run_id: UUID) -> ReviewResultRecord | None: ...
    async def finalize_review(
        self, token: UUID, result: ReviewResultRecord, *, backend_session_id: str | None
    ) -> RunRecord: ...


class LifecycleStore(Protocol):
    async def held_operations(self) -> tuple[HeldOperation, ...]: ...
    async def acquire_foreground(self, session_id: UUID) -> ForegroundLease: ...
    async def release_foreground(self, token: UUID) -> None: ...
    async def claim_cleanup(self) -> CleanupClaim | None: ...
    async def finish_cleanup(self, token: UUID, *, succeeded: bool) -> None: ...
