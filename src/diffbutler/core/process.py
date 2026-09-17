"""Recover ownership only when local host reboot proves prior processes ended."""

from typing import Protocol
from uuid import UUID

from diffbutler.domain.process import HostBoot, OperationKind, ProcessLaunch
from diffbutler.domain.review import ReviewConflict
from diffbutler.store.base import LifecycleStore, ProcessStore, ReviewStore


class HostBootReader(Protocol):
    async def current(self) -> HostBoot: ...


class ProcessRecovery:
    def __init__(
        self,
        processes: ProcessStore,
        reviews: ReviewStore,
        lifecycle: LifecycleStore,
        host: HostBootReader,
    ) -> None:
        self.processes = processes
        self.reviews = reviews
        self.lifecycle = lifecycle
        self.host = host

    async def prepare_launch(self, token: UUID, kind: OperationKind) -> ProcessLaunch:
        """Commit before spawn; a prior intent must never cause a second launch."""
        launch = ProcessLaunch(token, kind, await self.host.current())
        await self.processes.reserve_launch(launch)
        return launch

    async def recover_rebooted(self) -> tuple[UUID, ...]:
        """Release old-boot local owners only; never start reviews or kill PIDs.

        Same-boot crashes, unknown/migrated owners, and database moves to another
        host remain held. Call only for local operations, not remote supervisors.
        """
        operations = await self.lifecycle.held_operations()
        if not operations:
            return ()
        current = await self.host.current()
        recovered: list[UUID] = []
        for operation in operations:
            launch = await self.processes.get_launch(operation.token)
            if (
                launch is None
                or launch.kind != operation.kind
                or not launch.ended_by_reboot(current)
            ):
                continue
            try:
                if launch.kind == "background":
                    await self.reviews.release_review(launch.token)
                elif launch.kind == "foreground":
                    await self.lifecycle.release_foreground(launch.token)
                else:
                    await self.lifecycle.finish_cleanup(launch.token, succeeded=False)
            except ReviewConflict:
                # Another recovery worker consumed this immutable token.
                continue
            recovered.append(launch.token)
        return tuple(recovered)
