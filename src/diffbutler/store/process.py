"""Claim-protected launch metadata. Historical records never authorize a new launch."""

from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from diffbutler.domain.process import HostBoot, OperationKind, ProcessLaunch
from diffbutler.domain.review import ReviewConflict
from diffbutler.store.database import Database
from diffbutler.store.models import CleanupClaim, ForegroundLease, RunClaim
from diffbutler.store.models import ProcessLaunch as LaunchModel


def record(row: LaunchModel) -> ProcessLaunch:
    return ProcessLaunch(
        row.token,
        cast(OperationKind, row.kind),
        HostBoot(row.host_id, row.boot_id),
        row.launcher_pid,
    )


class SQLiteProcessStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def _owned(self, db: AsyncSession, token: UUID, kind: OperationKind) -> None:
        model = {"background": RunClaim, "foreground": ForegroundLease, "cleanup": CleanupClaim}[
            kind
        ]
        if await db.get(model, token) is None:
            raise ReviewConflict("Operation no longer owns its claim")

    async def get_launch(self, token: UUID) -> ProcessLaunch | None:
        async with self.database.sessions() as db:
            row = await db.get(LaunchModel, token)
            return None if row is None else record(row)

    async def reserve_launch(self, launch: ProcessLaunch) -> None:
        if launch.launcher_pid is not None:
            raise ValueError("Launch intent must be saved before starting a process")
        async with self.database.sessions() as db:
            connection = await db.connection()
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await self._owned(db, launch.token, launch.kind)
            row = await db.get(LaunchModel, launch.token)
            if row is not None:
                # Never authorize another spawn on an existing intent, even if a
                # crash prevented saving its PID. Recovery must resolve that gap.
                raise ReviewConflict("Launch intent already exists")
            db.add(
                LaunchModel(
                    token=launch.token,
                    kind=launch.kind,
                    host_id=launch.origin.host_id,
                    boot_id=launch.origin.boot_id,
                )
            )
            await db.commit()

    async def bind_launcher(self, token: UUID, pid: int) -> None:
        if pid < 1:
            raise ValueError("Launcher PID must be positive")
        async with self.database.sessions() as db:
            connection = await db.connection()
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            row = await db.get(LaunchModel, token)
            if row is None:
                raise ReviewConflict("Launch intent does not exist")
            await self._owned(db, token, cast(OperationKind, row.kind))
            if row.launcher_pid not in {None, pid}:
                raise ReviewConflict("Launcher identity cannot be replaced")
            row.launcher_pid = pid
            await db.commit()
