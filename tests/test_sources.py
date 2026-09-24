"""Offline source mediation against disposable Git repositories and fake Git."""

import asyncio
import os
import shutil
import signal
import sys
from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest

from diffbutler.domain.review import Side, SourceFile
from diffbutler.reviewers.base import SourceReader
from diffbutler.sources.git import GitSourceReader, SourceError, SourceLimits


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["gone", "signalled", "denied"])
async def test_macos_group_cleanup_retries_zombie_race_but_not_permanent_denial(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from diffbutler.sources.git import _terminate_group  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(sys, "platform", "darwin")
    calls = 0

    def killpg(pid: int, sig: int) -> None:
        nonlocal calls
        assert pid == 12345 and sig == signal.SIGKILL
        calls += 1
        if calls == 1 or outcome == "denied":
            raise PermissionError("synthetic permission failure")
        if outcome == "gone":
            raise ProcessLookupError

    async def yield_to_reaper(delay: float) -> None:
        pass

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(asyncio, "sleep", yield_to_reaper)
    if outcome == "denied":
        with pytest.raises(PermissionError):
            await _terminate_group(12345)
    else:
        await _terminate_group(12345)
    assert calls > 1


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
    with pytest.raises(SourceError, match="Submodule"):
        async with aclosing(reader.read_blobs()) as blobs:
            await anext(blobs)
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
async def test_large_unchanged_file_is_available_in_snapshots(tmp_path: Path) -> None:
    from diffbutler.core.snapshot import write_snapshot

    repo, _, _ = await repository_at_two_commits(tmp_path)
    content = b"plain source data\n" * 125_000
    (repo / "large.txt").write_bytes(content)
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "large source")
    base = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    (repo / "changed.py").write_text("another change\n")
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "small change")
    head = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    assert await reader.read_file(SourceFile("large.txt", Side.HEAD)) == content
    destination = tmp_path / "snapshot"
    await write_snapshot(reader, destination)
    for side in ("base", "head"):
        assert (destination / side / "large.txt").read_bytes() == content


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_reap_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    created: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def track_spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*argv, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track_spawn)
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
    task = asyncio.create_task(reader._git("cat-file"))  # pyright: ignore[reportPrivateUsage]
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
    # A short timeout may kill Python before it writes its marker. Check the
    # actual spawned PID in either case, without assuming interpreter startup speed.
    assert len(created) == 1 and created[0].returncode is not None
    pid = created[0].pid
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
    with pytest.raises(SourceError, match="local source objects"):
        async with aclosing(reader.read_blobs()) as blobs:
            async for _ in blobs:
                pass
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["stderr", "environment"])
async def test_git_capture_errors_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    behavior: str,
) -> None:
    executable = tmp_path / "fake-git"
    code = {
        "stderr": "sys.stderr.write('SYNTHETIC_SECRET'); sys.exit(1)",
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
        assert await reader._git() == b"ok\n"  # pyright: ignore[reportPrivateUsage]
    else:
        with pytest.raises(SourceError) as raised:
            await reader._git()  # pyright: ignore[reportPrivateUsage]
        assert "SYNTHETIC_SECRET" not in str(raised.value)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_source_timeout(timeout: float) -> None:
    with pytest.raises(ValueError):
        SourceLimits(timeout_s=timeout)


@pytest.mark.asyncio
async def test_large_stderr_drains_owned_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "overflow-git"
    executable.write_text(f"#!{sys.executable}\nimport os\nos.write(2, b'x' * 2_000_000)\n")
    executable.chmod(0o700)
    spawn = asyncio.create_subprocess_exec
    children: list[asyncio.subprocess.Process] = []

    async def capture(*args: str, **_kwargs: object) -> asyncio.subprocess.Process:
        child = await spawn(
            *args,
            env={"PATH": os.defpath},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    reader = GitSourceReader(tmp_path, str(executable), SourceLimits(), {})
    async with asyncio.timeout(5):
        assert await reader._git() == b""  # pyright: ignore[reportPrivateUsage]
    child = children[0]
    assert child.returncode is not None
    assert child.stdout is not None and child.stdout.at_eof()
    assert child.stderr is not None and child.stderr.at_eof()


@pytest.mark.asyncio
async def test_diff_is_pinned_and_disables_drivers(tmp_path: Path) -> None:
    repo, base, _ = await repository_at_two_commits(tmp_path)
    # Compare text-only commits, with hostile attributes/config and a dirty tree.
    (repo / "odd\tname\n.py").unlink()
    (repo / "changed.py").write_text("after without newline")
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "text head")
    head = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    marker = tmp_path / "driver-executed"
    await git(repo, "config", "diff.external", f"touch {marker}")
    await git(repo, "config", "diff.hostile.textconv", f"touch {marker}")
    (repo / ".gitattributes").write_text("* diff=hostile\n")
    (repo / "changed.py").write_text("unrelated dirty source")
    await git(repo, "add", "changed.py")
    before = await git(repo, "status", "--porcelain=v1")
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    diff = await reader.read_diff()
    assert reader.base_sha == base and reader.head_sha == head
    assert "-before\n+after without newline\n" in diff
    assert "\\ No newline at end of file" in diff
    assert "deleted.py" in diff and "-deleted" in diff
    assert "unrelated dirty source" not in diff
    assert not marker.exists()
    assert await git(repo, "status", "--porcelain=v1") == before
    assert await reader.read_diff() == diff


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"\x00binary", b"invalid\xff"])
async def test_unsupported_diff_content_fails_closed(tmp_path: Path, content: bytes) -> None:
    repo, base, _ = await repository_at_two_commits(tmp_path)
    (repo / "odd\tname\n.py").write_bytes(content)
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "unsupported diff")
    head = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    with pytest.raises(SourceError, match="unsupported"):
        await reader.read_diff()


@pytest.mark.asyncio
async def test_diff_empty_mode_rename_and_submodule_changes(tmp_path: Path) -> None:
    repo, base, head = await repository_at_two_commits(tmp_path)
    unchanged = await GitSourceReader.open(repo, base_sha=head, head_sha=head)
    assert await unchanged.read_diff() == ""
    await git(repo, "mv", "surrounding.py", "renamed.py")
    await git(repo, "update-index", "--chmod=+x", "changed.py")
    await git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},submodule")
    await git(repo, "commit", "-m", "mode rename and gitlink")
    newer = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    reader = await GitSourceReader.open(repo, base_sha=head, head_sha=newer)
    diff = await reader.read_diff()
    assert "old mode 100644\nnew mode 100755" in diff
    assert "rename from" not in diff
    assert "--- a/surrounding.py\n+++ /dev/null" in diff
    assert "--- /dev/null\n+++ b/renamed.py" in diff
    assert f"+Subproject commit {base}" in diff


async def divergent_repository(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = tmp_path / "diverged"
    repo.mkdir()
    await git(repo, "init", "--template=", "-b", "main")
    (repo / "persistence.py").write_text("old persistence\n")
    (repo / "template.html").write_text("original template\n")
    await git(repo, "add", ".")
    await git(repo, "commit", "-m", "common ancestor")
    ancestor = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    await git(repo, "checkout", "-b", "topic")
    (repo / "persistence.py").write_text("new persistence\n")
    await git(repo, "commit", "-am", "PR persistence change")
    head = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    await git(repo, "checkout", "main")
    for number in range(4):
        (repo / "template.html").write_text(f"newer main template {number}\n")
        await git(repo, "commit", "-am", f"independent main change {number}")
    base = (await git(repo, "rev-parse", "HEAD")).decode().strip()
    return repo, ancestor, base, head


@pytest.mark.asyncio
async def test_diverged_base_excludes_target_only_changes_and_reads_ancestor(
    tmp_path: Path,
) -> None:
    repo, ancestor, base, head = await divergent_repository(tmp_path)
    reader = await GitSourceReader.open(repo, base_sha=base, head_sha=head)
    assert reader.base_sha == base and reader.comparison_base_sha == ancestor
    assert [file.path for file in reader.changed_files] == ["persistence.py"]
    diff = await reader.read_diff()
    assert "template.html" not in diff
    assert "-old persistence\n+new persistence" in diff
    assert await reader.read_file(SourceFile("template.html", Side.BASE)) == b"original template\n"
    # Moving branch refs cannot change this reader's pinned comparison.
    await git(repo, "update-ref", "refs/heads/main", head)
    assert await reader.read_diff() == diff


@pytest.mark.asyncio
async def test_shallow_history_never_falls_back_to_endpoint_diff(tmp_path: Path) -> None:
    repo, _, base, head = await divergent_repository(tmp_path)
    shallow = tmp_path / "shallow"
    shallow.mkdir()
    await git(shallow, "init", "--bare", "--template=")
    await git(shallow, "fetch", "--depth=1", repo.as_uri(), base, head)
    with pytest.raises(SourceError, match="complete Git history"):
        await GitSourceReader.open(shallow, base_sha=base, head_sha=head)


@pytest.mark.asyncio
async def test_unrelated_and_ambiguous_histories_fail_closed(tmp_path: Path) -> None:
    repo, ancestor, base, head = await divergent_repository(tmp_path)
    tree = (await git(repo, "rev-parse", f"{ancestor}^{{tree}}")).decode().strip()
    unrelated = (await git(repo, "commit-tree", tree, "-m", "unrelated root")).decode().strip()
    with pytest.raises(SourceError, match="merge base"):
        await GitSourceReader.open(repo, base_sha=unrelated, head_sha=head)
    # Two independent merges of the same parents have two best common ancestors.
    left = (
        (await git(repo, "commit-tree", tree, "-p", base, "-p", head, "-m", "left"))
        .decode()
        .strip()
    )
    right = (
        (await git(repo, "commit-tree", tree, "-p", head, "-p", base, "-m", "right"))
        .decode()
        .strip()
    )
    with pytest.raises(SourceError, match="unique PR merge base"):
        await GitSourceReader.open(repo, base_sha=left, head_sha=right)
