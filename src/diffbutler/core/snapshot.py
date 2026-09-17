"""Materialize pinned, redacted Git blobs as text/data, never run checkout hooks."""

from pathlib import Path

from diffbutler.domain.review import Side, SourceFile
from diffbutler.reviewers.redaction import redact_and_report
from diffbutler.reviewers.resources import settled_io
from diffbutler.sources.git import GitSourceReader, SourceError


async def write_snapshot(
    reader: GitSourceReader, destination: Path, *, max_bytes: int = 64_000_000
) -> None:
    """Destination is a new directory beneath an already registered owned resource.

    Symlinks are represented by their link text as regular files; submodules fail
    rather than silently inventing contents. No Git metadata or executable modes
    are copied. Native read-only permissions remain the agent's responsibility.
    """
    total = 0
    await settled_io(lambda: destination.mkdir(mode=0o700))
    for side in Side:
        root = destination / side.value
        await settled_io(lambda root=root: root.mkdir(mode=0o700))
        for name in reader.paths(side):
            content = await reader.read_file(SourceFile(name, side))
            assert content is not None
            total += len(content)
            if total > max_bytes:
                raise SourceError("Pinned sources exceed the snapshot byte budget")
            target = root / name
            redacted = redact_and_report(content)

            def write(target: Path = target, content: bytes = redacted) -> None:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("xb") as stream:
                    stream.write(content)
                target.chmod(0o600)

            await settled_io(write)
