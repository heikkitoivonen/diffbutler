"""Offline source mediation against disposable Git repositories and fake Git."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from diffbutler.domain.review import Side, SourceFile
from diffbutler.reviewers.base import SourceReader
from diffbutler.sources.git import GitSourceReader, SourceError, SourceLimits


async def git(repository: Path, *args: str) -> bytes:
    executable = shutil.which("git")
    assert executable is not None
    process = await asyncio.create_subprocess_exec(
        executable,
        "-C",
        str(repository),
        *args,
        env={
            "PATH": os.defpath,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await process.communicate()
    assert process.returncode == 0, error.decode()
    return output


async def repository_at_two_commits(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repository"
    repo.mkdir()
    await git(repo, "init", "--template=")
    (repo / "changed.py").write_text("before\n")
    (repo / "surrounding.py").write_text("surrounding\n")
    (repo / "deleted.py").write_text("deleted\n")
    (repo / "link").symlink_to(tmp_path / "private")
    (tmp_path / "private").write_text("SYNTHETIC PRIVATE DATA")
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "base")
    base = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    (repo / "changed.py").write_text("after\n")
    (repo / "deleted.py").unlink()
    (repo / "odd\tname\n.py").write_bytes(b"\x00binary\xff")
    (repo / "$(touch marker).py").write_text("literal filename\n")
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "head")
    head = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    return repo, base, head


@pytest.mark.asyncio
async def test_pinned_sources_ignore_worktree_index_and_inherited_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, head = await repository_at_two_commits(tmp_path)
    (repo / "changed.py").write_text("unrelated staged changes\n")
    await git(repo, "add", "changed.py")
    (repo / "changed.py").write_text("unrelated unstaged changes\n")
    (repo / "untracked").write_text("unrelated\n")
    before = await git(repo, "status", "--porcelain=v1")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.repositoryformatversion")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "1000")
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    source: SourceReader = reader
    assert await source.read_file(SourceFile("changed.py", Side.BASE)) == b"before\n"
    assert await source.read_file(SourceFile("changed.py", Side.HEAD)) == b"after\n"
    assert await source.read_file(SourceFile("surrounding.py", Side.HEAD)) == b"surrounding\n"
    assert await source.read_file(SourceFile("deleted.py", Side.HEAD)) is None
    assert await source.read_file(SourceFile("deleted.py", Side.BASE)) == b"deleted\n"
    assert await source.read_file(SourceFile("untracked", Side.HEAD)) is None
    assert "surrounding.py" in reader.paths(Side.HEAD)
    assert {item.path for item in reader.changed_files} == {
        "changed.py",
        "deleted.py",
        "odd\tname\n.py",
        "$(touch marker).py",
    }
    assert await git(repo, "status", "--porcelain=v1") == before
    # Moving the checkout does not move the bound source revisions.
    await git(repo, "checkout", "--detach", base, "--force")
    assert await source.read_file(SourceFile("changed.py", Side.HEAD)) == b"after\n"


@pytest.mark.asyncio
async def test_links_binary_and_literal_paths(tmp_path: Path) -> None:
    repo, base, head = await repository_at_two_commits(tmp_path)
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    assert (
        await reader.read_file(SourceFile("link", Side.HEAD)) == str(tmp_path / "private").encode()
    )
    assert await reader.read_file(SourceFile("link/child", Side.HEAD)) is None
    assert await reader.read_file(SourceFile("odd\tname\n.py", Side.HEAD)) == b"\x00binary\xff"
    assert (
        await reader.read_file(SourceFile("$(touch marker).py", Side.HEAD)) == b"literal filename\n"
    )
    assert not (repo / "marker").exists()
    assert await reader.read_file(SourceFile(".git/config", Side.HEAD)) is None


@pytest.mark.asyncio
async def test_submodules_and_mode_changes(tmp_path: Path) -> None:
    repo, base, head = await repository_at_two_commits(tmp_path)
    await git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},submodule")
    await git(repo, "update-index", "--chmod=+x", "surrounding.py")
    await git(repo, "commit", "-m", "gitlink and executable bit")
    newer = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    reader = await GitSourceReader.open(repo, base_sha=head, head_sha=newer)
    assert {item.path for item in reader.changed_files} == {"submodule", "surrounding.py"}
    with pytest.raises(SourceError, match="Submodule"):
        await reader.read_file(SourceFile("submodule", Side.HEAD))
    assert await reader.read_file(SourceFile("submodule/private", Side.HEAD)) is None


@pytest.mark.asyncio
async def test_replacements_and_source_filters_are_not_used(tmp_path: Path) -> None:
    repo, base, head = await repository_at_two_commits(tmp_path)
    await git(repo, "replace", head, base)
    marker = tmp_path / "executed"
    await git(repo, "config", "filter.hostile.smudge", f"touch {marker}")
    await git(repo, "config", "diff.hostile.textconv", f"touch {marker}")
    (repo / ".gitattributes").write_text("* filter=hostile diff=hostile\n")
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    assert await reader.read_file(SourceFile("changed.py", Side.HEAD)) == b"after\n"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_missing_invalid_and_noncommit_revisions(tmp_path: Path) -> None:
    repo, base, head = await repository_at_two_commits(tmp_path)
    for invalid in ("HEAD", "--help", head[:12], head + ":changed.py", "../outside"):
        with pytest.raises(ValueError, match="object IDs"):
            await GitSourceReader.open(repo, base_sha=base, head_sha=invalid)
    for missing in ("0" * 40, (await git(repo, "rev-parse", "HEAD:changed.py")).decode().strip()):
        with pytest.raises(SourceError):
            await GitSourceReader.open(repo, base_sha=base, head_sha=missing)


@pytest.mark.asyncio
async def test_byte_limits_reject_instead_of_truncating(tmp_path: Path) -> None:
    repo, base, head = await repository_at_two_commits(tmp_path)
    with pytest.raises(SourceError, match="byte limit"):
        await GitSourceReader.open(
            repo, base_sha=base, head_sha=head, limits=SourceLimits(max_tree_bytes=10)
        )
    reader = await GitSourceReader.open(
        repo, base_sha=base, head_sha=head, limits=SourceLimits(max_file_bytes=6)
    )
    assert await reader.read_file(SourceFile("changed.py", Side.HEAD)) == b"after\n"
    with pytest.raises(SourceError, match="byte limit"):
        await reader.read_file(SourceFile("surrounding.py", Side.HEAD))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_reap_process(tmp_path: Path, cancel: bool) -> None:
    pid_file = tmp_path / "pid"
    executable = tmp_path / "fake-git"
    executable.write_text(
        f"#!{sys.executable}\nimport os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    executable.chmod(0o700)
    reader = GitSourceReader(
        tmp_path, str(executable), SourceLimits(timeout_s=0.3 if not cancel else 10), {}
    )
    task = asyncio.create_task(reader._git("cat-file", limit=100))  # pyright: ignore[reportPrivateUsage]
    if cancel:
        async with asyncio.timeout(5):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(SourceError, match="timed out"):
            await task
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_missing_promisor_blob_does_not_launch_remote_helper(tmp_path: Path) -> None:
    repo, base, head = await repository_at_two_commits(tmp_path)
    oid = (await git(repo, "rev-parse", "HEAD:changed.py")).decode().strip()
    marker = tmp_path / "remote-executed"
    await git(repo, "config", "remote.origin.url", f"ext::touch {marker}")
    await git(repo, "config", "remote.origin.promisor", "true")
    await git(repo, "config", "extensions.partialClone", "origin")
    await git(repo, "config", "protocol.ext.allow", "always")
    (repo / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    with pytest.raises(SourceError, match="local source objects"):
        await reader.read_file(SourceFile("changed.py", Side.HEAD))
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["stderr", "overflow", "environment"])
async def test_git_capture_limits_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    behavior: str,
) -> None:
    executable = tmp_path / "fake-git"
    code = {
        "stderr": "sys.stderr.write('SYNTHETIC_SECRET'); sys.exit(1)",
        "overflow": "sys.stderr.write('x' * 1000000)",
        "environment": (
            "assert not any(k in os.environ for k in "
            "['GITHUB_TOKEN', 'SSH_AUTH_SOCK', 'HOME', 'GIT_DIR']); "
            "assert os.environ['GIT_ALLOW_PROTOCOL'] == ''; print('ok')"
        ),
    }[behavior]
    executable.write_text(f"#!{sys.executable}\nimport os, sys\n{code}\n")
    executable.chmod(0o700)
    monkeypatch.setenv("GITHUB_TOKEN", "SYNTHETIC_SECRET")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/synthetic/agent")
    reader = GitSourceReader(tmp_path, str(executable), SourceLimits(), {})
    if behavior == "environment":
        assert await reader._git(limit=100) == b"ok\n"  # pyright: ignore[reportPrivateUsage]
    else:
        with pytest.raises(SourceError) as raised:
            await reader._git(limit=100)  # pyright: ignore[reportPrivateUsage]
        assert "SYNTHETIC_SECRET" not in str(raised.value)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_source_timeout(timeout: float) -> None:
    with pytest.raises(ValueError):
        SourceLimits(timeout_s=timeout)
