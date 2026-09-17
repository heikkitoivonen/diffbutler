"""Immutable public review records. Explicit starts and worker claims never launch a process."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from diffbutler.domain.review import (
    AnalysisIdentity,
    ReviewPolicy,
    RunClaim,
    RunStatus,
    SessionStatus,
)
from diffbutler.store.base import ReviewStore


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


class ReviewService:
    """Internal scheduling seam; callers map explicit UI actions to requests.

    No executor is connected until background isolation is implemented. Worker
    claims are durable and never expire automatically: process death must be
    established before releasing ownership. Successful output handling is deferred.
    """

    def __init__(self, store: ReviewStore, policy: ReviewPolicy) -> None:
        self.store = store
        self.policy = policy

    async def get_run(self, run_id: UUID) -> AIRun:
        return AIRun.model_validate(await self.store.get_run(run_id), from_attributes=True)

    async def request_review(self, identity: AnalysisIdentity) -> AIRun:
        return AIRun.model_validate(
            await self.store.request_review(identity, self.policy), from_attributes=True
        )

    async def claim_review(
        self, run_id: UUID, *, capacity: int = 2, paused: bool = False
    ) -> RunClaim | None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if paused:
            return None
        return await self.store.claim_review(run_id, capacity=capacity, policy=self.policy)

    async def cancel_review(self, run_id: UUID) -> AIRun:
        return AIRun.model_validate(await self.store.cancel_review(run_id), from_attributes=True)

    async def release_review(self, token: UUID) -> AIRun:
        """Acknowledge worker exit; unfinished analysis becomes a failed attempt."""
        return AIRun.model_validate(await self.store.release_review(token), from_attributes=True)
