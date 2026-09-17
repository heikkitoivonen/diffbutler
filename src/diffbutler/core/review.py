"""Immutable public review records. No start/dispatch service is implemented yet."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from diffbutler.domain.review import RunStatus, SessionStatus


class ReviewSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    task_id: UUID
    reviewer_id: str
    generation: int = Field(ge=1)
    display_name: str
    backend_session_id: str | None
    status: SessionStatus
    created_at: datetime


class AIRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    task_id: UUID
    session_id: UUID
    reviewer_id: str
    head_sha: str
    base_sha: str
    prompt_version: str
    config_hash: str
    analysis_key: str
    attempt: int = Field(ge=1)
    status: RunStatus
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
