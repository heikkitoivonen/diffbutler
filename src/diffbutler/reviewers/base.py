"""Background-only adapter seam. Contracts alone do not enforce process isolation."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from diffbutler.domain.diff import DiffIndex
from diffbutler.domain.forge import PullRequest
from diffbutler.domain.review import ChangedFile, SourceFile


class SourceReader(Protocol):
    """Pinned PR source: BASE means the comparison merge base, HEAD the PR head."""

    async def read_file(self, source: SourceFile) -> bytes | None: ...


class ReviewSources(SourceReader, Protocol):
    """Prepared sources pinned to the PR revisions."""

    @property
    def base_sha(self) -> str: ...

    @property
    def comparison_base_sha(self) -> str: ...

    @property
    def head_sha(self) -> str: ...

    @property
    def changed_files(self) -> tuple[ChangedFile, ...]: ...

    @property
    def limitations(self) -> tuple[str, ...]: ...

    async def read_diff(self) -> str: ...


@dataclass(frozen=True)
class ReviewBudget:
    timeout_s: int = 900

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise ValueError("review budgets must be positive")


@dataclass(frozen=True)
class ReviewContext:
    pr: PullRequest
    session_id: UUID
    run_id: UUID
    diff: str
    changed_files: tuple[ChangedFile, ...]
    sources: SourceReader
    instructions: str
    schema_json: str
    budget: ReviewBudget
    comparison_base_sha: str
    diff_index: DiffIndex | None = None
    checkout_path: Path | None = None
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewerCapabilities:
    resume: bool = False
    discussion: bool = False
    delete_session: bool = False


@dataclass(frozen=True)
class ReviewProgress:
    text: str


@dataclass(frozen=True)
class ReviewOutput:
    """Untrusted output, not parsed findings or evidence of forge publication."""

    stdout: bytes
    stderr: bytes
    exit_code: int
    backend_session_id: str | None = None


type ReviewEvent = ReviewProgress | ReviewOutput


class Reviewer(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def capabilities(self) -> ReviewerCapabilities: ...

    def review(self, context: ReviewContext) -> AsyncIterator[ReviewEvent]:
        """Stream captured progress and one final output; never own a terminal."""
        ...
