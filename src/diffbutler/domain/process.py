"""Durable launch intent; a launcher PID alone never proves process-tree exit."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

OperationKind = Literal["background", "foreground", "cleanup"]


@dataclass(frozen=True)
class HostBoot:
    host_id: UUID
    boot_id: UUID


@dataclass(frozen=True)
class ProcessLaunch:
    token: UUID
    kind: OperationKind
    origin: HostBoot
    launcher_pid: int | None = None

    def __post_init__(self) -> None:
        if self.launcher_pid is not None and self.launcher_pid < 1:
            raise ValueError("Launcher PID must be positive")

    def ended_by_reboot(self, current: HostBoot) -> bool:
        # A different machine may still have the original process running.
        return self.origin.host_id == current.host_id and self.origin.boot_id != current.boot_id
