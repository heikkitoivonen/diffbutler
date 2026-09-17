"""Offline real filesystem cleanup, with SQLite ownership and restart coverage."""

import asyncio
import shutil
import threading
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.test_review_claims import seed, service
from tests.test_review_results import captured
from tests.test_sync import PR, REF

from diffbutler.core.lifecycle import LifecycleService
from diffbutler.core.resources import provision_directory
from diffbutler.core.sync import DiffButler
from diffbutler.domain.lifecycle import CleanupSession
from diffbutler.domain.resources import OwnedDirectory
from diffbutler.domain.review import ReviewConflict
from diffbutler.reviewers.resources import OwnedDirectories, OwnedSessionCleaner
from diffbutler.store.database import Database
from diffbutler.store.lifecycle import SQLiteLifecycleStore
from diffbutler.store.models import ReviewSession
from diffbutler.store.queue import SQLiteQueueStore
from diffbutler.store.review import SQLiteReviewStore

pytestmark = pytest.mark.asyncio


def cleanup_session(resource: OwnedDirectory, *, backend: str | None = None) -> CleanupSession:
    return CleanupSession(
        resource.session_id,
        "fake",
        backend,
        None,
        (tuple(resource.manifest().items()),),
    )


async def test_cleanup_is_confined_idempotent_and_preserves_external_symlink_target(
    tmp_path: Path,
) -> None:
    directories = await OwnedDirectories.initialize(tmp_path / "managed")
    resource = directories.reserve(uuid4())
    other = directories.reserve(uuid4())
    path = await directories.create(resource)
    other_path = await directories.create(other)
    outside = tmp_path / "repository"
    outside.mkdir()
    (outside / "important").write_text("keep")
    (path / "link").symlink_to(outside, target_is_directory=True)
    (path / "artifact").write_text("temporary")
    cleaner = OwnedSessionCleaner(directories, {})
    await cleaner.cleanup(cleanup_session(resource))
    await cleaner.cleanup(cleanup_session(resource))
    assert not path.exists() and other_path.is_dir()
    assert (outside / "important").read_text() == "keep"


async def test_unknown_manifest_backend_and_wrong_owner_fail_before_deletion(
    tmp_path: Path,
) -> None:
    directories = await OwnedDirectories.initialize(tmp_path / "managed")
    resource = directories.reserve(uuid4())
    path = await directories.create(resource)
    cleaner = OwnedSessionCleaner(directories, {})
    session = cleanup_session(resource)
    for invalid in (
        replace(session, id=uuid4()),
        replace(session, backend_session_id="../../repository"),
        replace(session, transcript_ref="/repository/transcript"),
        replace(session, resources=((("kind", "path"), ("path", str(tmp_path))),)),
        cleanup_session(replace(resource, root_id=uuid4())),
    ):
        with pytest.raises(ValueError):
            await cleaner.cleanup(invalid)
        assert path.is_dir()


async def test_existing_directory_is_not_adopted_and_replaced_root_is_rejected(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileExistsError):
        await OwnedDirectories.initialize(tmp_path)
    directories = await OwnedDirectories.initialize(tmp_path / "managed")
    resource = directories.reserve(uuid4())
    path = await directories.create(resource)
    with pytest.raises(FileExistsError):
        await directories.create(resource)
    moved = tmp_path / "moved"
    directories.path.rename(moved)
    directories.path.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OSError):
        await directories.delete(resource)
    assert (moved / path.name).is_dir()


async def test_resource_symlink_never_follows_directory_outside_root(tmp_path: Path) -> None:
    directories = await OwnedDirectories.initialize(tmp_path / "managed")
    resource = directories.reserve(uuid4())
    outside = tmp_path / "outside"
    outside.mkdir()
    (directories.path / resource.name).symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        await directories.delete(resource)
    assert outside.is_dir()


async def test_cancelled_io_settles_before_releasing_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    finish = threading.Event()

    directories = await OwnedDirectories.initialize(tmp_path / "managed")
    resource = directories.reserve(uuid4())
    original = shutil.rmtree

    def work(path: str, *, dir_fd: int) -> None:
        started.set()
        assert finish.wait(timeout=5)
        original(path, dir_fd=dir_fd)

    monkeypatch.setattr(work, "avoids_symlink_attacks", True, raising=False)
    monkeypatch.setattr(shutil, "rmtree", work)
    path = await directories.create(resource)
    task = asyncio.create_task(directories.delete(resource))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not path.exists()


class Backend:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_session(self, backend_session_id: str) -> None:
        self.deleted.append(backend_session_id)


async def test_manifest_before_creation_restart_completion_and_cleanup(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/resources.db"
    database = Database(url)
    await database.initialize()
    directories = await OwnedDirectories.initialize(tmp_path / "managed")
    try:
        forge, identity = await seed(database)
        reviews = service(database)
        store = SQLiteReviewStore(database)
        run = await reviews.request_review(identity)
        claim = await reviews.claim_review(run.id)
        assert claim is not None
        with pytest.raises(ReviewConflict):
            await provision_directory(store, directories, token=uuid4(), session_id=run.session_id)
        assert list(directories.path.iterdir()) == [directories.path / ".diffbutler-root"]
        resource = directories.reserve(run.session_id)
        await store.register_resource(claim.token, resource)
        await store.register_resource(claim.token, resource)
        # Simulate interruption after manifest commit and before directory creation.
        path = await provision_directory(
            store,
            directories,
            token=claim.token,
            session_id=run.session_id,
        )
        (path / "transcript").write_text("private conversation")
        async with database.sessions() as db:
            saved = await db.scalar(select(ReviewSession))
            assert saved is not None and len(saved.resource_manifest_json) == 2
        await reviews.finalize_review(claim.token, captured())
        forge.prs[REF] = replace(PR, state="closed")
        await DiffButler(SQLiteQueueStore(database), forge).sync()
    finally:
        await database.close()
    reopened = Database(url)
    try:
        backend = Backend()
        cleaner = OwnedSessionCleaner(
            OwnedDirectories(directories.path, directories.root_id),
            {"fake": backend},
        )
        # Seed uses reviewer "fake"; cleanup dispatch is determined by that identity.
        lifecycle = LifecycleService(SQLiteLifecycleStore(reopened), cleaner)
        assert await lifecycle.cleanup_once()
        assert not path.exists()
        assert backend.deleted == ["fake-id"]
        result = await service(reopened).get_result(run.id)
        assert result is not None and result.document is not None and result.stdout is None
        assert (await service(reopened).get_session(run.session_id)).status == "cleaned"
    finally:
        await reopened.close()
