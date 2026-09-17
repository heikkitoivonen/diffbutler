"""UI-independent foreground ownership and automatic completed-task cleanup."""

from typing import Protocol
from uuid import UUID

from diffbutler.domain.lifecycle import CleanupSession, ForegroundLease, HeldOperation
from diffbutler.store.base import LifecycleStore


class SessionCleaner(Protocol):
    async def cleanup(self, session: CleanupSession) -> None:
        """Idempotently delete only session-owned data, raising if unsupported.

        Return only after all deletion operations have settled. A partial failure
        may be retried with the same manifest. Never delete the repository home or
        infer a backend filesystem path from its opaque conversation ID.
        """
        ...


class LifecycleService:
    def __init__(self, store: LifecycleStore, cleaner: SessionCleaner) -> None:
        self.store = store
        self.cleaner = cleaner

    async def held_operations(self) -> tuple[HeldOperation, ...]:
        return await self.store.held_operations()

    async def acknowledge_cleanup_exit(self, token: UUID) -> None:
        """After verified orphan operation exit, schedule an idempotent retry."""
        await self.store.finish_cleanup(token, succeeded=False)

    async def acquire_foreground(self, session_id: UUID) -> ForegroundLease:
        """Explicit human resume action; no launch, re-review, or posting authorization."""
        return await self.store.acquire_foreground(session_id)

    async def release_foreground(self, token: UUID) -> None:
        """Only after verified process-tree exit or an established launch failure."""
        await self.store.release_foreground(token)

    async def cleanup_once(self) -> bool:
        """Consume one durable completion job without another user action.

        Ordinary failures schedule a retry; cancellation deliberately leaves the
        claim held until the supervisor establishes operation exit. A new service
        cannot steal an orphan claim based only on age. No scheduler is installed
        by constructing this service.
        """
        claim = await self.store.claim_cleanup()
        if claim is None:
            return False
        try:
            for session in claim.sessions:
                await self.cleaner.cleanup(session)
        except Exception:
            # Persist only a fixed diagnostic; backend errors may contain secrets.
            await self.store.finish_cleanup(claim.token, succeeded=False)
        else:
            await self.store.finish_cleanup(claim.token, succeeded=True)
        return True
