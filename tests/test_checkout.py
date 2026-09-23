"""Native Git preparation uses pinned objects, without local working-tree state."""

from pathlib import Path

import pytest
from tests.test_sources import git

from diffbutler.core.workspaces import materialize_checkout
from diffbutler.sources.git import GitSourceReader


@pytest.mark.asyncio
async def test_checkout_preserves_source_and_ignores_local_configuration(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    await git(repo, "init")
    await git(repo, "config", "user.name", "Test")
    await git(repo, "config", "user.email", "test@example.invalid")
    source = repo / "script"
    source.write_text("before\n")
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "base")
    base = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    original = "TOKEN=ghp_" + "a" * 30 + "\n"
    source.write_text(original)
    source.chmod(0o755)
    (repo / "link").symlink_to("script")
    (repo / ".gitattributes").write_text("script filter=trap\n")
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "head")
    head = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    await git(repo, "config", "filter.trap.smudge", "false")
    await git(repo, "config", "filter.trap.required", "true")
    hook = repo / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    source.write_text("dirty\n")
    (repo / "untracked").write_text("local")
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    checkout = tmp_path / "checkout"
    await materialize_checkout(reader, checkout)
    assert (checkout / "script").read_text() == original
    assert (checkout / "script").stat().st_mode & 0o111
    assert (checkout / "link").is_symlink()
    assert not (checkout / "untracked").exists()
    assert await git(checkout, "show", "diffbutler-base:script") == b"before\n"
    assert await git(checkout, "symbolic-ref", "--short", "HEAD") == b"diffbutler-pr\n"
    assert not await git(checkout, "status", "--porcelain")
    assert (checkout / ".git/objects/info/alternates").is_file()
    assert source.read_text() == "dirty\n"
