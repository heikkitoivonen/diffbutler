"""Immutable ownership and cleanup records, independent of processes and storage."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class ForegroundLease:
    token: UUID
    session_id: UUID
    backend_session_id: str
    head_sha: str
    task_version: int
    acquired_at: datetime


@dataclass(frozen=True)
class CleanupSession:
    id: UUID
    reviewer_id: str
    backend_session_id: str | None
    transcript_ref: str | None
    resources: tuple[tuple[tuple[str, str], ...], ...]


@dataclass(frozen=True)
class CleanupClaim:
    token: UUID
    job_id: UUID
    sessions: tuple[CleanupSession, ...]


@dataclass(frozen=True)
class HeldOperation:
    token: UUID
    kind: str
    resource_id: UUID
    acquired_at: datetime
