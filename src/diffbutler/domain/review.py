"""Immutable identities and source descriptions for background analysis; no I/O."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from uuid import UUID


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


class SessionStatus(StrEnum):
    READY = "ready"
    SUSPENDED = "suspended"
    CLEANED = "cleaned"


@dataclass(frozen=True)
class SessionRecord:
    id: UUID
    task_id: UUID
    reviewer_id: str
    generation: int
    display_name: str
    backend_session_id: str | None
    status: SessionStatus
    created_at: datetime


class Side(StrEnum):
    BASE = "base"
    HEAD = "head"


@dataclass(frozen=True)
class SourceFile:
    """A repository-relative file at one of the reviewed revisions."""

    path: str
    side: Side

    def __post_init__(self) -> None:
        path = PurePosixPath(self.path)
        if (
            not self.path
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
            or "\\" in self.path
            or "\x00" in self.path
        ):
            raise ValueError("source path must be a normalized relative repository path")


@dataclass(frozen=True)
class ChangedFile:
    path: str
    old_path: str | None = None

    def __post_init__(self) -> None:
        SourceFile(self.path, Side.HEAD)
        if self.old_path is not None:
            SourceFile(self.old_path, Side.BASE)


@dataclass(frozen=True)
class AnalysisIdentity:
    """Task identity includes the account and cycle; retries share a key."""

    task_id: UUID
    head_sha: str
    reviewer_id: str
    prompt_version: str
    config_hash: str

    def __post_init__(self) -> None:
        if not all((self.head_sha, self.reviewer_id, self.prompt_version, self.config_hash)):
            raise ValueError("analysis identity components must be nonempty")

    @property
    def key(self) -> str:
        payload = json.dumps(
            [
                str(self.task_id),
                self.head_sha,
                self.reviewer_id,
                self.prompt_version,
                self.config_hash,
            ],
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class ComparisonCheck:
    base_sha: str
    matches: bool


class ComparisonChanged(Exception):
    """The target moved while its comparison was being checked; recheck it."""


class ReviewConflict(ValueError):
    """The requested task or run no longer permits this action."""


@dataclass(frozen=True)
class RunRecord:
    id: UUID
    task_id: UUID
    session_id: UUID
    reviewer_id: str
    head_sha: str
    base_sha: str
    prompt_version: str
    config_hash: str
    analysis_key: str
    attempt: int
    status: RunStatus
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    status_reason: str | None = None


@dataclass(frozen=True)
class RunClaim:
    """Exclusive worker ownership, retained until the worker acknowledges exit."""

    token: UUID
    run: RunRecord


@dataclass(frozen=True)
class ReviewPolicy:
    reviewer_ids: frozenset[str]
    disabled_repositories: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ReviewResultRecord:
    """Captured result and retained analysis; no adapter or ORM objects cross stores."""

    exit_code: int
    stdout: bytes | None
    stderr: bytes | None
    document_json: str | None
    parse_tier: str | None
    error: str | None
    notes: tuple[str, ...]
    selected_indices: tuple[int, ...]
    min_confidence: float
    findings_threshold: str
    anchors_json: str | None = None
    anchor_base_sha: str | None = None
    anchor_head_sha: str | None = None
