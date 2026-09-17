"""Immutable public review records. Explicit starts and worker claims never launch a process."""

import math
from datetime import datetime
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from diffbutler.domain.review import (
    AnalysisIdentity,
    ReviewPolicy,
    ReviewResultRecord,
    RunClaim,
    RunStatus,
    SessionStatus,
)
from diffbutler.reviewers.base import ReviewOutput
from diffbutler.reviewers.parsing import ReviewDocument, Severity, parse_review
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


class ReviewResult(BaseModel):
    model_config = ConfigDict(frozen=True, ser_json_bytes="base64", val_json_bytes="base64")

    run_id: UUID
    exit_code: int
    stdout: bytes | None
    stderr: bytes | None
    document: ReviewDocument | None
    parse_tier: str | None
    error: str | None
    notes: tuple[str, ...]
    selected_indices: tuple[int, ...]
    min_confidence: float
    findings_threshold: Severity


class ReviewService:
    """Internal scheduling seam; callers map explicit UI actions to requests.

    No executor is connected until background isolation is implemented. Worker
    claims are durable and never expire automatically: process death must be
    established before releasing ownership or finalizing a captured result.
    """

    def __init__(self, store: ReviewStore, policy: ReviewPolicy) -> None:
        self.store = store
        self.policy = policy

    async def get_run(self, run_id: UUID) -> AIRun:
        return AIRun.model_validate(await self.store.get_run(run_id), from_attributes=True)

    async def get_session(self, session_id: UUID) -> ReviewSession:
        """Read durable conversation metadata; does not resume or start a process."""
        return ReviewSession.model_validate(
            await self.store.get_session(session_id), from_attributes=True
        )

    async def bind_backend_session(self, token: UUID, backend_session_id: str) -> ReviewSession:
        """Checkpoint a backend identity while the caller still owns its worker claim."""
        return ReviewSession.model_validate(
            await self.store.bind_backend_session(token, backend_session_id), from_attributes=True
        )

    async def suspend_session(self, session_id: UUID) -> ReviewSession:
        """Mark an idle conversation suspended; never stop a process or delete data."""
        return ReviewSession.model_validate(
            await self.store.suspend_session(session_id), from_attributes=True
        )

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

    async def get_result(self, run_id: UUID) -> ReviewResult | None:
        result = await self.store.get_result(run_id)
        if result is None:
            return None
        return ReviewResult(
            run_id=run_id,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            document=(
                ReviewDocument.model_validate_json(result.document_json)
                if result.document_json is not None
                else None
            ),
            parse_tier=result.parse_tier,
            error=result.error,
            notes=result.notes,
            selected_indices=result.selected_indices,
            min_confidence=result.min_confidence,
            findings_threshold=cast(Severity, result.findings_threshold),
        )

    async def finalize_review(
        self,
        token: UUID,
        output: ReviewOutput,
        *,
        min_confidence: float = 0.5,
        findings_threshold: Severity = "minor",
        max_output_bytes: int = 4_000_000,
    ) -> AIRun:
        """Acknowledge verified worker exit and finalize already-extracted text.

        A future executor must establish process-tree death before calling this.
        Parsed anchors are not yet validated for publication. No forge is contacted.
        """
        ranks = {"question": 0, "nit": 1, "minor": 2, "important": 3, "blocking": 4}
        if not math.isfinite(min_confidence) or not 0 <= min_confidence <= 1:
            raise ValueError("minimum confidence must be finite and between zero and one")
        if findings_threshold not in ranks:
            raise ValueError("unknown findings threshold")
        if max_output_bytes < 1:
            raise ValueError("output budget must be positive")
        if len(output.stdout) + len(output.stderr) > max_output_bytes:
            raise ValueError("output exceeds persistence byte budget")
        parsed = parse_review(output, max_output_bytes=max_output_bytes)
        selected = (
            tuple(
                index
                for index, finding in enumerate(parsed.document.findings)
                if finding.confidence >= min_confidence
                and ranks[finding.severity] >= ranks[findings_threshold]
            )
            if parsed.document is not None
            else ()
        )
        result = ReviewResultRecord(
            output.exit_code,
            output.stdout,
            output.stderr,
            parsed.document.model_dump_json() if parsed.document is not None else None,
            parsed.tier,
            parsed.error,
            parsed.notes,
            selected,
            min_confidence,
            findings_threshold,
        )
        return AIRun.model_validate(
            await self.store.finalize_review(
                token,
                result,
                backend_session_id=output.backend_session_id,
            ),
            from_attributes=True,
        )
