"""Offline shared-object reuse, ownership, and retention with disposable Git."""

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from tests.test_native import prepared
from tests.test_sources import git
from tests.test_sync import REF

from diffbutler.core.repositories import RepositoryCache, file_lock
from diffbutler.domain.forge import PRRef
from diffbutler.reviewers.resources import OwnedDirectories


@pytest.mark.asyncio
async def test_local_objects_are_independent_and_local_checkout_is_untouched(
    tmp_path: Path,
) -> None:
    settings, forge = await prepared(tmp_path)
    local = tmp_path / "repository"
    before = await git(local, "show-ref")
    pr = forge.prs[REF]
    cache = RepositoryCache(settings.general.repository_root)
    session, run = uuid4(), uuid4()
    path, transport = await cache.prepare(
        REF, session, run, pr.base_sha, pr.head_sha, local_path=local
    )
    assert transport is None
    assert await git(local, "show-ref") == before
    assert (local / "changed.py").read_text() == "dirty unrelated work\n"
    assert not (path / "objects/info/alternates").exists()
    (local / "changed.py").write_text("new revision\n")
    await git(local, "commit", "-am", "new head")
    new_head = (await git(local, "rev-parse", "HEAD")).decode().strip()
    next_run = uuid4()
    reused, transport = await cache.prepare(
        REF, session, next_run, pr.base_sha, new_head, local_path=local
    )
    assert reused == path and transport is None
    old_ref = f"refs/diffbutler/{session.hex}/{run.hex}/head"
    assert (await git(path, "rev-parse", old_ref)).decode().strip() == pr.head_sha
    new_ref = f"refs/diffbutler/{session.hex}/{next_run.hex}/head"
    assert (await git(path, "rev-parse", new_ref)).decode().strip() == new_head
    local.rename(tmp_path / "moved-local")
    assert await git(path, "show", f"{pr.head_sha}:changed.py") == b"after\n"
    assert await git(path, "rev-parse", f"refs/diffbutler/{session.hex}/{run.hex}/base")
    # Forge hostname is part of the cache identity, not just owner/repository.
    assert await cache.existing(replace(REF, forge="github:https://elsewhere.example")) is None


@pytest.mark.asyncio
async def test_concurrent_preparation_fetches_once_and_retention_protects_each_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, forge = await prepared(tmp_path)
    pr = forge.prs[REF]
    remote = tmp_path / "repository"
    calls = 0

    async def fetch(path: Path, ref: PRRef, base: str, head: str, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        await git(path, "init", "--bare", "--template=")
        await git(path, "fetch", "--depth=1", remote.as_uri(), base, head)
        return "https"

    monkeypatch.setattr("diffbutler.core.repositories.fetch_repository", fetch)
    first, second = uuid4(), uuid4()
    cache = RepositoryCache(settings.general.repository_root, retention_days=1)
    results = await asyncio.gather(
        cache.prepare(REF, first, uuid4(), pr.base_sha, pr.head_sha),
        RepositoryCache(cache.path).prepare(
            replace(REF, number=2), second, uuid4(), pr.base_sha, pr.head_sha
        ),
    )
    assert calls == 1 and results[0][0] == results[1][0]
    path = results[0][0]
    os.utime(path / "last-used", (0, 0))
    await cache.prune()
    assert path.exists()
    await cache.release(first)
    os.utime(path / "last-used", (0, 0))
    await cache.prune()
    assert path.exists()
    refs = (await git(path, "for-each-ref", "--format=%(refname)")).decode()
    assert first.hex not in refs and second.hex in refs
    await cache.release(second)
    await cache.prune()
    assert path.exists()  # Retention starts when the last session releases it.
    os.utime(path / "last-used", (0, 0))
    await cache.prune()
    assert not path.exists()
    assert remote.exists()


@pytest.mark.asyncio
async def test_cancelled_lock_waiter_does_not_hold_lock(tmp_path: Path) -> None:
    path = tmp_path / "lock"

    async def wait() -> None:
        async with file_lock(path):
            pytest.fail("Waiter acquired an already held lock")

    async with file_lock(path):
        task = asyncio.create_task(wait())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with asyncio.timeout(1), file_lock(path):
        pass


@pytest.mark.asyncio
async def test_cache_refuses_unowned_roots(tmp_path: Path) -> None:
    settings, forge = await prepared(tmp_path)
    cache = RepositoryCache(settings.general.repository_root)
    cache.path.mkdir(parents=True)
    unrelated = cache.path / "important"
    unrelated.write_text("keep")
    pr = forge.prs[REF]
    with pytest.raises(FileNotFoundError):
        await cache.prepare(REF, uuid4(), uuid4(), pr.base_sha, pr.head_sha)
    assert unrelated.read_text() == "keep"


@pytest.mark.asyncio
async def test_cache_refuses_another_kind_of_owned_root(tmp_path: Path) -> None:
    root = await OwnedDirectories.initialize(tmp_path / "homes")
    resource = root.reserve(uuid4())
    home = await root.create(resource)
    (home / "conversation").write_text("keep")
    with pytest.raises(FileNotFoundError):
        await RepositoryCache(root.path, retention_days=0).prune()
    assert (home / "conversation").read_text() == "keep"
