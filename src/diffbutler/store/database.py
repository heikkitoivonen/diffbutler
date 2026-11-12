"""Async SQLite startup and packaged migrations, with consistent pre-upgrade backups."""

import asyncio
import os
import sqlite3
import stat
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from uuid import uuid4

import aiosqlite
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, event, inspect
from sqlalchemy.engine import URL, AdaptedConnection, make_url
from sqlalchemy.exc import ArgumentError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

# PRAGMA user_version once content freed before secure_delete has been rewritten.
SCRUBBED_VERSION = 1
_NEW_PRIVATE_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


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
            "db.url: Only sqlite+aiosqlite is supported; PostgreSQL/MySQL are not implemented"
        )
    if url.username or url.password or url.host or url.port or url.query:
        raise DatabaseError(
            "db.url: SQLite requires a local file path without credentials or query options"
        )
    if not url.database or url.database == ":memory:":
        raise DatabaseError("db.url: use a persistent SQLite file")
    return url.set(database=str(Path(url.database).expanduser().absolute()))


async def _finish[T](task: asyncio.Future[T]) -> tuple[T, bool]:
    """Finish owned I/O despite cancellation, reporting it to the resource owner."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def connect_sqlite(path: Path) -> aiosqlite.Connection:
    # Cancelling aiosqlite's initial await can discard the connection its worker
    # thread is still opening, before SQLAlchemy can own or dispose of it.
    connection, cancelled = await _finish(
        asyncio.ensure_future(aiosqlite.connect(path, check_same_thread=False))
    )
    if cancelled:
        await _finish(asyncio.create_task(connection.close()))
        raise asyncio.CancelledError
    return connection


async def configure_sqlite(connection: aiosqlite.Connection) -> None:
    """Enable WAL without blocking the event loop during startup contention."""
    try:
        async with connection.cursor() as cursor:
            await cursor.execute("PRAGMA busy_timeout=5000")
            await cursor.execute("PRAGMA foreign_keys=ON")
            # Cleanup deletes raw agent output; overwrite freed pages so it is gone.
            await cursor.execute("PRAGMA secure_delete=ON")
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                try:
                    await cursor.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    # SQLite may return BUSY immediately on a journal-mode lock
                    # upgrade, even with busy_timeout set. Retry after yielding.
                    if (
                        getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY
                        or asyncio.get_running_loop().time() >= deadline
                    ):
                        raise
                    await asyncio.sleep(0.05)
    except BaseException:
        # Connect-event failures can occur before SQLAlchemy owns a pool record.
        await connection.close()
        raise


def _configure_sqlite(connection: AdaptedConnection, _record: object) -> None:
    connection.run_async(configure_sqlite)


def private_files(path: Path, *, create: bool) -> None:
    """Keep the database and its WAL owner-only; it holds private PR content."""
    # A configured path may be a symlink; SQLite opens, and names sidecars after, its target.
    target = path.resolve()
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(FileExistsError):
            os.close(os.open(target, _NEW_PRIVATE_FILE, 0o600))
    for name in {path, target}:
        for suffix in ("", "-wal", "-shm"):
            candidate = name.with_name(name.name + suffix)
            try:
                status = os.lstat(candidate)
            except FileNotFoundError:
                continue
            mode = stat.S_IMODE(status.st_mode)
            if stat.S_ISREG(status.st_mode) and status.st_uid == os.getuid() and mode & 0o077:
                os.chmod(candidate, mode & 0o700)


def backup_sqlite(path: Path) -> Path:
    """Save a consistent, owner-only snapshot beside the database before changing its schema."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = path.with_name(f"{path.name}.{stamp}.{uuid4().hex}.bak")
    # VACUUM INTO reads committed WAL contents and, unlike the backup API, leaves
    # out free pages that may still hold deleted raw output.
    try:
        os.close(os.open(destination, _NEW_PRIVATE_FILE, 0o600))
        with closing(sqlite3.connect(path)) as source:
            source.execute("VACUUM INTO ?", (str(destination),))
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
        self.engine: AsyncEngine = create_async_engine(
            self.url, async_creator=partial(connect_sqlite, self.path)
        )
        event.listen(self.engine.sync_engine, "connect", _configure_sqlite)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self, *, auto_migrate: bool = True) -> DatabaseStatus:
        existed = self.path.exists()
        if not auto_migrate and not existed:
            raise DatabaseError("database is missing; enable db.auto_migrate to initialize it")
        await asyncio.to_thread(private_files, self.path, create=auto_migrate)
        status = await self._migrate(auto_migrate=auto_migrate, existed=existed)
        if auto_migrate:
            await self._scrub_freed_pages()
        return status

    async def _migrate(self, *, auto_migrate: bool, existed: bool) -> DatabaseStatus:
        async with self.engine.connect() as connection:
            # Serialize startup migrations across processes. No tables are changed
            # until a consistent backup has completed successfully.
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            current = await connection.run_sync(
                lambda conn: MigrationContext.configure(conn).get_current_heads()
            )
            scripts = ScriptDirectory.from_config(migration_config())
            head = scripts.get_current_head()
            assert head is not None
            if current == (head,):
                await connection.commit()
                return DatabaseStatus(head, False, None)
            known = {revision.revision for revision in scripts.walk_revisions()}
            if current and (len(current) != 1 or current[0] not in known):
                raise DatabaseError(
                    "database revision is unknown to this version; "
                    "use the matching DiffButler release"
                )
            tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
            if not current and set(tables) - {"alembic_version"}:
                raise DatabaseError(
                    "database has unversioned tables; refusing to adopt or overwrite it"
                )
            if not auto_migrate:
                raise DatabaseError("database migrations are not current; enable db.auto_migrate")
            backup = await asyncio.to_thread(backup_sqlite, self.path) if existed else None
            await connection.run_sync(lambda conn: command.upgrade(migration_config(conn), "head"))
            await connection.commit()
            return DatabaseStatus(head, True, backup)

    async def _scrub_freed_pages(self) -> None:
        """Rewrite the file once so content deleted before secure_delete is not recoverable."""
        async with self.engine.connect() as connection:
            result = await connection.exec_driver_sql("PRAGMA user_version")
            if result.scalar_one() >= SCRUBBED_VERSION:
                return
            try:
                await connection.exec_driver_sql("VACUUM")
                # In WAL mode the rewrite reaches the main file only at checkpoint.
                result = await connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
                busy, *_ = result.one()
            except OperationalError:
                return  # Another process holds the database; retry on a later start.
            if busy:
                return  # A reader blocked the checkpoint; retry on a later start.
            await connection.exec_driver_sql(f"PRAGMA user_version={SCRUBBED_VERSION}")
            await connection.commit()

    async def truncate_wal(self) -> None:
        """Best effort: drop WAL frames that may still hold overwritten content."""
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")

    async def close(self) -> None:
        # Cancellation during disposal can interrupt aiosqlite.close after the
        # pool has discarded the connection. Finish closing it before propagating
        # cancellation, including repeated cancellation during app shutdown.
        _, cancelled = await _finish(asyncio.create_task(self.engine.dispose()))
        if cancelled:
            raise asyncio.CancelledError
