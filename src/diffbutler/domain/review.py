"""Immutable identities and source descriptions for background analysis; no I/O."""

import hashlib
import json
from dataclasses import dataclass
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
