"""Mediated Git object reads. This is not a sandbox for an agent executable."""

import asyncio
import logging
import math
import os
import re
import shutil
import signal
import sys
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from diffbutler.domain.review import ChangedFile, Side, SourceFile
from diffbutler.sources.diagnostics import GitTrace


class SourceError(Exception):
    """Safe source diagnostic; never include Git stderr or local credentials."""


async def _terminate_group(pid: int) -> None:
    # Darwin returns EPERM for a group containing only unreaped zombies. Give
    # the child watcher time to reap, then require successful signalling or ESRCH.
    # Never interpret persistent EPERM as proof that the group has exited.
    for attempt in range(100):
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            if sys.platform != "darwin" or attempt == 99:
                raise
            await asyncio.sleep(0.01)


@dataclass(frozen=True)
class SourceLimits:
    timeout_s: float = 30

    def __post_init__(self) -> None:
        if self.timeout_s <= 0 or not math.isfinite(self.timeout_s):
            raise ValueError("source limits must be positive")


@dataclass(frozen=True)
class _Entry:
    mode: str
    oid: str


class GitSourceReader:
    """Read only blobs present in two pinned commits from a trusted local repo.

    Symlinks return their stored target text, never the target's contents. Gitlinks
    are explicitly unavailable. No checkout, fetch, filters, or project execution.
    Local Git administration is trusted; untrusted repository *contents* are data.
    """

    def __init__(
        self,
        repository: Path,
        executable: str,
        limits: SourceLimits,
        trees: Mapping[Side, Mapping[str, _Entry]],
    ) -> None:
        self._repository = repository
        self._executable = executable
        self._limits = limits
        self._trees = trees
        self._base_sha = ""
        self._head_sha = ""
        self._comparison_base_sha = ""

    async def common_directory(self) -> Path:
        """Locate the trusted object store, including for linked worktrees."""
        raw = await self._git("rev-parse", "--path-format=absolute", "--git-common-dir")
        return Path(os.fsdecode(raw.rstrip(b"\n")))

    @classmethod
    async def open(
        cls,
        repository: Path,
        *,
        base_sha: str,
        head_sha: str,
        limits: SourceLimits | None = None,
    ) -> GitSourceReader:
        # Full object IDs only: no ref movement, revision syntax, or option injection.
        for revision in (base_sha, head_sha):
            if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision) is None:
                raise ValueError("source revisions must be full lowercase Git object IDs")
        limits = limits or SourceLimits()
        executable = shutil.which("git")
        if executable is None:
            raise SourceError("Git is unavailable")
        reader = cls(repository.absolute(), executable, limits, {})
        for revision in (base_sha, head_sha):
            if await reader._git("cat-file", "-t", revision) != b"commit\n":
                raise SourceError("Source revision is not a commit")
        if await reader._git("rev-parse", "--is-shallow-repository") != b"false\n":
            raise SourceError("Review requires complete Git history; prepare the repository again")
        try:
            raw_bases = await reader._git("merge-base", "--all", base_sha, head_sha)
            bases = raw_bases.decode("ascii").split()
        except SourceError:
            raise SourceError("Could not establish the PR merge base from local history") from None
        if len(bases) != 1 or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", bases[0]) is None:
            raise SourceError("Review requires a unique PR merge base")
        reader._comparison_base_sha = bases[0]
        trees: dict[Side, Mapping[str, _Entry]] = {}
        for side, revision in ((Side.BASE, reader.comparison_base_sha), (Side.HEAD, head_sha)):
            raw = await reader._git(
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                revision,
            )
            entries: dict[str, _Entry] = {}
            try:
                for record in raw.split(b"\0"):
                    if not record:
                        continue
                    metadata, name = record.split(b"\t", 1)
                    mode, kind, oid = metadata.decode("ascii").split(" ")
                    path = name.decode("utf-8")
                    SourceFile(path, side)
                    if (
                        (mode, kind)
                        not in {
                            ("100644", "blob"),
                            ("100755", "blob"),
                            ("120000", "blob"),
                            ("160000", "commit"),
                        }
                        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) is None
                        or path in entries
                    ):
                        raise ValueError("invalid tree entry")
                    entries[path] = _Entry(mode, oid)
            except ValueError, UnicodeError:
                raise SourceError("Source tree contains unsupported paths or entries") from None
            trees[side] = MappingProxyType(entries)
        reader._trees = MappingProxyType(trees)
        reader._base_sha = base_sha
        reader._head_sha = head_sha
        return reader

    @property
    def base_sha(self) -> str:
        return self._base_sha

    @property
    def comparison_base_sha(self) -> str:
        """Unique merge base used for BASE source, changed files, and diff hunks."""
        return self._comparison_base_sha

    @property
    def head_sha(self) -> str:
        return self._head_sha

    @property
    def limitations(self) -> tuple[str, ...]:
        return (
            f"PR diff and base-side source use merge base {self.comparison_base_sha}.",
            "Renames are represented as deletion and addition; submodule contents are unavailable.",
            "Binary and non-UTF-8 diff content is summarized without line anchors; "
            "original file bytes remain available.",
        )

    async def read_diff(self) -> str:
        """Merge-base-to-head diff with summaries for binary/non-UTF-8 changes.

        Force text so working-tree attributes cannot hide changes as binary or
        invoke custom drivers. No textconv, external diff, or project execution.
        """
        if not self.base_sha or not self.head_sha:
            raise SourceError("Source reader has not been prepared")
        raw = await self._git(
            "diff-tree",
            "-r",
            "-p",
            "--no-commit-id",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--text",
            "--no-color",
            "--no-relative",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--unified=3",
            "--diff-algorithm=myers",
            "--no-indent-heuristic",
            "--ignore-submodules=none",
            "--submodule=short",
            self.comparison_base_sha,
            self.head_sha,
            "--",
        )
        sections: list[str] = []
        # Hunk lines always have a prefix, and Git quotes newlines in paths,
        # so only file boundaries can begin with this unprefixed header.
        for section in re.split(rb"(?=^diff --git )", raw, flags=re.MULTILINE):
            try:
                text = section.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and "\x00" not in text:
                sections.append(text)
                continue
            headers = re.search(rb"^--- (.+)\n\+\+\+ (.+)\n", section, re.MULTILINE)
            if headers is None:
                raise SourceError("Source diff contains unsupported file headers")
            # Preserve modes and object IDs, but omit the entire file's hunks:
            # partially decoded binary data must never create line anchors.
            summary = (
                section[: headers.start()]
                + b"Binary files "
                + headers[1].removesuffix(b"\t")
                + b" and "
                + headers[2].removesuffix(b"\t")
                + b" differ\n"
            )
            try:
                sections.append(summary.decode("utf-8"))
            except UnicodeDecodeError:
                raise SourceError("Source diff contains unsupported file headers") from None
        return "".join(sections)

    def paths(self, side: Side) -> tuple[str, ...]:
        """All tracked paths, including unchanged surrounding sources."""
        return tuple(sorted(self._trees[side]))

    @property
    def changed_files(self) -> tuple[ChangedFile, ...]:
        """Renames are represented as deletion plus addition; mode changes count."""
        base, head = self._trees[Side.BASE], self._trees[Side.HEAD]
        return tuple(
            ChangedFile(path)
            for path in sorted(base.keys() | head.keys())
            if base.get(path) != head.get(path)
        )

    async def read_file(self, source: SourceFile) -> bytes | None:
        entry = self._trees[source.side].get(source.path)
        if entry is None:
            return None
        if entry.mode == "160000":
            raise SourceError("Submodule source is unavailable in this repository context")
        # The object ID came from the pinned tree, not from a caller's expression.
        return await self._git("cat-file", "blob", entry.oid)

    async def read_blobs(self) -> AsyncGenerator[tuple[tuple[SourceFile, ...], bytes]]:
        """Stream each distinct pinned blob once; callers must close the generator.

        Object contents are streamed without a repository-sized cache. Requests contain
        only verified object IDs, never filenames or revision expressions.
        Lazy fetch stays disabled.
        """
        objects: dict[str, list[SourceFile]] = {}
        for side in Side:
            for path, entry in self._trees[side].items():
                if entry.mode == "160000":
                    raise SourceError("Submodule source is unavailable in this repository context")
                objects.setdefault(entry.oid, []).append(SourceFile(path, side))
        if not objects:
            return
        env = self._environment()
        argv = (
            self._executable,
            "--no-pager",
            "-C",
            str(self._repository),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "gc.auto=0",
            "cat-file",
            "--batch",
        )
        trace = GitTrace(logging.getLogger(__name__), argv, env)
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        )
        errors: asyncio.Task[None] | None = None
        outcome = "interrupted"

        async def drain(stream: asyncio.StreamReader, *, diagnostic: bool = False) -> None:
            while chunk := await stream.read(65536):
                if diagnostic:
                    trace.capture(chunk)

        async def cleanup() -> None:
            try:
                process = await spawn
            except OSError:
                trace.finish(None, outcome)
                return
            await _terminate_group(process.pid)
            assert (
                process.stdin is not None
                and process.stdout is not None
                and process.stderr is not None
            )
            process.stdin.close()
            # Drain after killing, even if cancellation interrupted the initial spawn.
            await asyncio.gather(
                drain(process.stdout),
                errors if errors is not None else drain(process.stderr, diagnostic=True),
                return_exceptions=True,
            )
            await process.wait()
            trace.finish(process.returncode, outcome)

        try:
            process = await asyncio.shield(spawn)
            assert (
                process.stdin is not None
                and process.stdout is not None
                and process.stderr is not None
            )
            errors = asyncio.create_task(drain(process.stderr, diagnostic=True))
            for oid, sources in objects.items():
                async with asyncio.timeout(self._limits.timeout_s):
                    process.stdin.write(oid.encode("ascii") + b"\n")
                    await process.stdin.drain()
                    header = await process.stdout.readline()
                    fields = header.rstrip(b"\n").split(b" ")
                    if (
                        len(fields) != 3
                        or fields[0] != oid.encode("ascii")
                        or fields[1] != b"blob"
                        or not fields[2].isdigit()
                    ):
                        raise SourceError(
                            "Git batch could not read the requested local source objects"
                        )
                    content = await process.stdout.readexactly(int(fields[2]))
                    if await process.stdout.readexactly(1) != b"\n":
                        raise SourceError("Git batch returned an invalid source object boundary")
                yield tuple(sources), content
            process.stdin.close()
            async with asyncio.timeout(self._limits.timeout_s):
                await asyncio.shield(errors)
                status = await process.wait()
            outcome = "exited"
            if status != 0:
                raise SourceError("Git batch source retrieval failed")
        except TimeoutError:
            outcome = "timeout"
            raise SourceError("Git batch source retrieval timed out") from None
        except asyncio.IncompleteReadError, OSError, ValueError:
            raise SourceError(
                "Git batch could not read the requested local source objects"
            ) from None
        finally:
            closing = asyncio.create_task(cleanup())
            cancelled = False
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    cancelled = True
            closing.result()
            if cancelled:
                raise asyncio.CancelledError

    @staticmethod
    def _environment() -> dict[str, str]:
        # No inherited tokens, Git overrides, HOME configuration, or SSH agents.
        # Protocol denial also protects Git versions without GIT_NO_LAZY_FETCH.
        return {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_OPTIONAL_LOCKS": "0",
        }

    async def _git(self, *args: str) -> bytes:
        env = self._environment()
        argv = (
            self._executable,
            "--no-pager",
            "-C",
            str(self._repository),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "gc.auto=0",
            *args,
        )
        trace = GitTrace(logging.getLogger(__name__), argv, env)
        outcome = "interrupted"
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            trace.finish(None, f"os-error errno={exc.errno}")
            raise SourceError("Could not start Git source retrieval") from None
        assert process.stdout is not None and process.stderr is not None

        async def collect(stream: asyncio.StreamReader, *, diagnostic: bool = False) -> bytes:
            result = bytearray()
            while chunk := await stream.read(65536):
                result.extend(chunk)
                if diagnostic:
                    trace.capture(chunk)
            return bytes(result)

        stdout = asyncio.create_task(collect(process.stdout))
        stderr = asyncio.create_task(collect(process.stderr, diagnostic=True))
        try:
            async with asyncio.timeout(self._limits.timeout_s):
                output, _ = await asyncio.gather(stdout, stderr)
                status = await process.wait()
                outcome = "exited"
                if status != 0:
                    raise SourceError("Git could not read the requested local source objects")
                return output
        except TimeoutError:
            outcome = "timeout"
            raise SourceError("Git source retrieval timed out") from None
        finally:
            # Terminate the owned group even if a child retained the output pipes.
            await _terminate_group(process.pid)
            stdout.cancel()
            stderr.cancel()
            await asyncio.gather(stdout, stderr, return_exceptions=True)

            async def discard(stream: asyncio.StreamReader) -> None:
                while await stream.read(65536):
                    pass

            # Cancellation can leave a paused pipe transport with buffered data.
            # Drain after killing so EOF closes both transports before returning.
            await asyncio.gather(discard(process.stdout), discard(process.stderr))
            await process.wait()
            trace.finish(process.returncode, outcome)
