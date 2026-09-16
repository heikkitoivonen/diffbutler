"""Async SQLite startup and packaged migrations, with consistent pre-upgrade backups."""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, event, inspect
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


class DatabaseError(ValueError):
    """A safe diagnostic that does not expose connection credentials."""


@dataclass(frozen=True)
class DatabaseStatus:
    revision: str
    migrated: bool
    backup: Path | None


def migration_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def sqlite_url(value: str) -> URL:
    try:
        url = make_url(value)
    except ArgumentError, ValueError:
        raise DatabaseError("db.url: invalid SQLAlchemy URL") from None
    if url.drivername != "sqlite+aiosqlite":
        raise DatabaseError(
            "db.url: M0 supports sqlite+aiosqlite only; PostgreSQL/MySQL are deferred"
        )
    if url.username or url.password or url.host or url.port or url.query:
        raise DatabaseError(
            "db.url: SQLite requires a local file path without credentials or query options"
        )
    if not url.database or url.database == ":memory:":
        raise DatabaseError("db.url: use a persistent SQLite file")
    return url.set(database=str(Path(url.database).expanduser().absolute()))


def _configure_sqlite(connection: Any, _record: Any) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def backup_sqlite(path: Path) -> Path:
    """Save a consistent snapshot beside the database before changing its schema."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = path.with_name(f"{path.name}.{stamp}.{uuid4().hex}.bak")
    # SQLite's backup API includes committed WAL contents, unlike copying the file.
    try:
        with (
            closing(sqlite3.connect(path)) as source,
            closing(sqlite3.connect(destination)) as target,
        ):
            source.backup(target)
    except sqlite3.Error:
        destination.unlink(missing_ok=True)
        raise DatabaseError(
            "database backup failed; check file permissions and SQLite integrity"
        ) from None
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


class Database:
    """Owns the engine/session factory; callers must close it after use."""

    def __init__(self, url: str) -> None:
        self.url = sqlite_url(url)
        assert self.url.database is not None
        self.path = Path(self.url.database)
        self.engine: AsyncEngine = create_async_engine(self.url)
        event.listen(self.engine.sync_engine, "connect", _configure_sqlite)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self, *, auto_migrate: bool = True) -> DatabaseStatus:
        existed = self.path.exists()
        if not auto_migrate and not existed:
            raise DatabaseError("database is missing; enable db.auto_migrate to initialize it")
        if auto_migrate:
            await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)
        async with self.engine.connect() as connection:
            # Serialize startup migrations across processes. No tables are changed
            # until a consistent backup has completed successfully.
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            current = await connection.run_sync(
                lambda conn: MigrationContext.configure(conn).get_current_heads()
            )
            head = ScriptDirectory.from_config(migration_config()).get_current_head()
            assert head is not None
            if current == (head,):
                await connection.commit()
                return DatabaseStatus(head, False, None)
            if current:
                raise DatabaseError(
                    "database revision is unknown to this version; "
                    "use the matching DiffButler release"
                )
            tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
            if set(tables) - {"alembic_version"}:
                raise DatabaseError(
                    "database has unversioned tables; refusing to adopt or overwrite it"
                )
            if not auto_migrate:
                raise DatabaseError("database migrations are not current; enable db.auto_migrate")
            backup = await asyncio.to_thread(backup_sqlite, self.path) if existed else None
            await connection.run_sync(lambda conn: command.upgrade(migration_config(conn), "head"))
            await connection.commit()
            return DatabaseStatus(head, True, backup)

    async def close(self) -> None:
        await self.engine.dispose()
