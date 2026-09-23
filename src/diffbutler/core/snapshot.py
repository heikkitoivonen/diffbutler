"""Materialize pinned, redacted Git blobs as text/data, never run checkout hooks."""

import logging
import time
from collections.abc import Callable
from contextlib import aclosing
from pathlib import Path

from diffbutler.domain.redaction import redact_source
from diffbutler.domain.review import Side, SourceFile
from diffbutler.reviewers.resources import settled_io
from diffbutler.sources.git import GitSourceReader


async def write_snapshot(
    reader: GitSourceReader,
    destination: Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Stream unique blobs into a new owned directory, with one redaction per blob.

    Symlinks become regular files containing link text; submodules fail. Files
    are independent copies, without Git administration or executable modes.
    """
    total = sum(len(reader.paths(side)) for side in Side)
    completed = objects = redactions = 0
    started = last_report = time.monotonic()

    def report() -> None:
        message = f"Preparing source snapshots: {completed}/{total} files ({objects} unique blobs)"
        logging.getLogger(__name__).info("%s", message)
        if progress is not None:
            progress(message)

    report()

    def initialize() -> None:
        destination.mkdir(mode=0o700)
        for side in Side:
            (destination / side.value).mkdir(mode=0o700)

    await settled_io(initialize)
    async with aclosing(reader.read_blobs()) as blobs:
        async for sources, content in blobs:

            def write(sources: tuple[SourceFile, ...] = sources, content: bytes = content) -> int:
                redacted = redact_source(content)
                for source in sources:
                    target = destination / source.side.value / source.path
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with target.open("xb") as stream:
                        stream.write(redacted.content)
                    target.chmod(0o600)
                return redacted.count

            redactions += await settled_io(write)
            completed += len(sources)
            objects += 1
            if time.monotonic() - last_report >= 5:
                report()
                last_report = time.monotonic()
    report()
    logging.getLogger(__name__).info(
        "Source snapshots ready: files=%d blobs=%d masked=%d elapsed_s=%.3f",
        completed,
        objects,
        redactions,
        time.monotonic() - started,
    )
