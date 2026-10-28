"""Offline migration, backup, and relational ledger tests."""

import asyncio
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from diffbutler.store.database import (
    Database,
    DatabaseError,
    DatabaseStatus,
    backup_sqlite,
    migration_config,
)
from diffbutler.store.models import (
    LOCAL_ACCOUNT_ID,
    Account,
    Base,
    CleanupJob,
    PullRequest,
    Repository,
    ReviewTask,
    utc_now,
)


@pytest.mark.asyncio
async def test_cancelled_database_connect_closes_late_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    opened = asyncio.Event()
    closing = asyncio.Event()
    release_open = threading.Event()
    release_close = threading.Event()
    closed = threading.Event()
    connections: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        def close(self) -> None:
            loop.call_soon_threadsafe(closing.set)
            assert release_close.wait(5)
            super().close()
            closed.set()

    def delayed_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = connect(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        loop.call_soon_threadsafe(opened.set)
        assert release_open.wait(5)
        return connection

    monkeypatch.setattr(sqlite3, "connect", delayed_connect)
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/cancelled-connect.db")
    task = asyncio.create_task(database.initialize())
    try:
        await asyncio.wait_for(opened.wait(), timeout=5)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
        release_open.set()
        await asyncio.wait_for(closing.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert closed.is_set()
    finally:
        release_open.set()
        release_close.set()
        await asyncio.gather(task, return_exceptions=True)
        await database.close()
        # Also clean up when run against the leaking implementation.
        for connection in connections:
            sqlite3.Connection.close(connection)


@pytest.mark.asyncio
async def test_database_close_finishes_before_propagating_repeated_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/closing.db")
    await database.initialize()
    started = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    connections: list[aiosqlite.Connection] = []
    original_close = aiosqlite.Connection.close

    async def delayed_close(connection: aiosqlite.Connection) -> None:
        connections.append(connection)
        started.set()
        await release.wait()
        await original_close(connection)
        closed.set()

    monkeypatch.setattr(aiosqlite.Connection, "close", delayed_close)
    task = asyncio.create_task(database.close())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
            assert not closed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert closed.is_set()
        assert "Exception closing connection" not in caplog.text
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        for connection in connections:
            await original_close(connection)
        await database.close()


@pytest.mark.asyncio
async def test_upgrade_reopen_and_schema_matches_models(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/nested/ledger.db"
    database = Database(url)
    try:
        status = await database.initialize()
        assert status.migrated and status.backup is None
        async with database.engine.connect() as connection:
            assert await connection.scalar(text("PRAGMA journal_mode")) == "wal"
            assert await connection.scalar(text("PRAGMA busy_timeout")) == 5000
            assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
            changes = await connection.run_sync(
                lambda conn: compare_metadata(MigrationContext.configure(conn), Base.metadata)
            )
            assert changes == []
        async with database.sessions() as session:
            account = await session.get(Account, LOCAL_ACCOUNT_ID)
            assert account is not None and account.login == "local"
    finally:
        await database.close()
    reopened = Database(url)
    try:
        status = await reopened.initialize(auto_migrate=False)
        assert not status.migrated and status.revision == "0011"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_round_trip_and_backup_before_existing_database_upgrade(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/ledger.db")
    try:
        await database.initialize()
        async with database.engine.begin() as connection:
            await connection.run_sync(
                lambda conn: command.downgrade(migration_config(conn), "base")
            )
            assert await connection.run_sync(lambda conn: inspect(conn).get_table_names()) == [
                "alembic_version"
            ]
        with pytest.raises(DatabaseError, match="not current"):
            await database.initialize(auto_migrate=False)
        status = await database.initialize()
        assert status.migrated and status.backup is not None
        with closing(sqlite3.connect(status.backup)) as backup:
            assert backup.execute("SELECT * FROM alembic_version").fetchall() == []
            assert (
                backup.execute("SELECT name FROM sqlite_master WHERE name='account'").fetchall()
                == []
            )
        assert (await database.initialize()).backup is None
        assert len(list(tmp_path.glob("*.bak"))) == 1
    finally:
        await database.close()


def test_backup_includes_committed_wal(tmp_path: Path) -> None:
    path = tmp_path / "wal.db"
    with closing(sqlite3.connect(path)) as source:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("CREATE TABLE marker (value TEXT)")
        source.execute("INSERT INTO marker VALUES ('committed')")
        source.commit()
        assert path.with_name("wal.db-wal").exists()
        with closing(sqlite3.connect(backup_sqlite(path))) as backup:
            assert backup.execute("SELECT value FROM marker").fetchone() == ("committed",)


@pytest.mark.asyncio
async def test_missing_database_is_not_created_when_migrations_disabled(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/missing/ledger.db")
    try:
        with pytest.raises(DatabaseError, match="missing"):
            await database.initialize(auto_migrate=False)
        assert not database.path.parent.exists()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unknown_revision_and_unmanaged_database_are_preserved(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/ledger.db")
    try:
        await database.initialize()
        async with database.engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num='future'"))
        with pytest.raises(DatabaseError, match="unknown"):
            await database.initialize()
        async with database.engine.begin() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version")) == "future"
            )
            await connection.execute(text("DROP TABLE alembic_version"))
        with pytest.raises(DatabaseError, match="unversioned"):
            await database.initialize()
        assert not list(tmp_path.glob("*.bak"))
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_backup_failure_prevents_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledger.db"
    path.touch()
    database = Database(f"sqlite+aiosqlite:///{path}")

    def fail_backup(_path: Path) -> Path:
        raise OSError("no space")

    monkeypatch.setattr("diffbutler.store.database.backup_sqlite", fail_backup)
    try:
        with pytest.raises(OSError, match="no space"):
            await database.initialize()
        async with database.engine.connect() as connection:
            assert await connection.run_sync(lambda conn: inspect(conn).get_table_names()) == []
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(10))
async def test_two_startups_share_one_migration(tmp_path: Path, iteration: int) -> None:
    first = Database(f"sqlite+aiosqlite:///{tmp_path}/ledger-{iteration}.db")
    second = Database(str(first.url))
    try:
        results = await asyncio.gather(
            first.initialize(), second.initialize(), return_exceptions=True
        )
        assert all(isinstance(result, DatabaseStatus) for result in results), results
        assert sum(result.migrated for result in results if isinstance(result, DatabaseStatus)) == 1
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_cycle_identity_foreign_keys_and_cleanup_ledger(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/ledger.db")
    try:
        await database.initialize()
        async with database.sessions() as session:
            repo = Repository(forge="github", namespace="acme", name="api", default_branch="main")
            session.add(repo)
            await session.flush()
            pr = PullRequest(
                repository_id=repo.id,
                number=1,
                title="Synthetic PR",
                author="author",
                head_sha="abc",
                base_sha="def",
                head_ref="topic",
                base_ref="main",
                url="https://example.invalid/pr/1",
                state="open",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(pr)
            await session.flush()
            task = ReviewTask(pull_request_id=pr.id)
            session.add(task)
            await session.flush()
            assert task.account_id == LOCAL_ACCOUNT_ID
            assert task.state == "PENDING_AI_REVIEW"
            assert task.request_sources_json == [] and task.following
            session.add(CleanupJob(review_task_id=task.id))
            await session.commit()
            # Restart-visible cleanup and account-scoped history identity exist.
            assert await session.scalar(select(CleanupJob.status)) == "pending"
            session.add(ReviewTask(pull_request_id=pr.id))
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
            session.add(ReviewTask(pull_request_id=uuid4()))
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_known_older_revision_upgrades_with_data_and_backup(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/upgrade.db")
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(lambda conn: command.upgrade(migration_config(conn), "0001"))
            await connection.execute(text("UPDATE account SET display_name='Retained identity'"))
        with pytest.raises(DatabaseError, match="not current"):
            await database.initialize(auto_migrate=False)
        status = await database.initialize()
        assert status.migrated and status.revision == "0011" and status.backup
        with closing(sqlite3.connect(status.backup)) as backup:
            assert backup.execute("SELECT version_num FROM alembic_version").fetchone() == ("0001",)
            assert backup.execute("SELECT display_name FROM account").fetchone() == (
                "Retained identity",
            )
        async with database.sessions() as session:
            assert await session.scalar(select(Account.display_name)) == "Retained identity"
    finally:
        await database.close()
