"""Worker orchestration for explicit requests, including native CLI execution."""

import asyncio
import math
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol
from uuid import UUID

from diffbutler.core.review import AIRun, ReviewService
from diffbutler.domain.review import ReviewConflict, RunClaim, RunRecord
from diffbutler.reviewers.base import ReviewContext, ReviewOutput
from diffbutler.reviewers.extraction import OutputFormat
from diffbutler.reviewers.parsing import ResponseFormat, Severity


class ReviewExecution(Protocol):
    async def run(
        self,
        context: ReviewContext,
        backend_session_id: str | None,
        checkpoint: Callable[[str], Awaitable[None]],
    ) -> ReviewOutput:
        """Capture output under the configured native read-only profile, never a terminal.

        Implementations must await checkpoint when a backend identity is learned.
        Creating the execution object must not start a process; only run may start
        it. A native adapter must reassert its permission profile and durable ownership,
        including resumes from formerly foreground conversations.
        """
        ...

    async def stop_and_wait(self) -> None:
        """Establish whole process-tree exit, including a spawn still in progress.

        Required on normal return too: protocol completion and EOF do not prove
        exit. Raise if exit cannot be established, leaving durable ownership held.
        This must also settle the run coroutine; no detached I/O may remain.
        """
        ...


async def _settle[T](task: asyncio.Task[T]) -> tuple[T, bool]:
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.done():
                return task.result(), True
            cancelled = True


class ReviewWorker:
    def __init__(
        self,
        reviews: ReviewService,
        prepare: Callable[[RunRecord], Awaitable[ReviewContext]],
        execution_factory: Callable[[RunClaim], ReviewExecution],
        *,
        before_prepare: Callable[[RunClaim], Awaitable[None]] | None = None,
        output_format: OutputFormat | None = None,
        response_format: ResponseFormat = "json",
        poll_interval: float = 0.25,
        min_confidence: float = 0.5,
        findings_threshold: Severity = "minor",
    ) -> None:
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll interval must be positive and finite")
        self.reviews = reviews
        self.before_prepare = before_prepare
        self.prepare = prepare
        self.execution_factory = execution_factory
        self.output_format = output_format
        self.response_format: ResponseFormat = response_format
        self.poll_interval = poll_interval
        self.min_confidence = min_confidence
        self.findings_threshold: Severity = findings_threshold

    async def execute_requested(
        self,
        run_id: UUID,
        *,
        capacity: int = 2,
        paused: bool = False,
    ) -> AIRun | None:
        """Consume one already requested run, never create or requeue analysis.

        Factory/prepare are trusted dependencies; native execution relies on the
        installed agent sandbox and process-group lifecycle contract. Cancellation
        releases ownership
        only after stop_and_wait; stop failure deliberately retains the claim.
        """
        claim = await self.reviews.claim_review(run_id, capacity=capacity, paused=paused)
        if claim is None:
            return None
        exited = True  # No execution object/process exists during preparation.
        try:
            if self.before_prepare is not None:
                await self.before_prepare(claim)
            context = await self.prepare(claim.run)
            if (context.run_id, context.session_id, context.pr.base_sha, context.pr.head_sha) != (
                claim.run.id,
                claim.run.session_id,
                claim.run.base_sha,
                claim.run.head_sha,
            ):
                raise ReviewConflict("Prepared context does not match worker ownership")
            if context.diff_index is None or (
                context.diff_index.base_sha,
                context.diff_index.head_sha,
            ) != (claim.run.base_sha, claim.run.head_sha):
                raise ReviewConflict("Worker requires a pinned original diff index")
            if not await self.reviews.claim_is_current(claim.token):
                return await self.reviews.release_review(claim.token)
            session = await self.reviews.get_session(claim.run.session_id)

            async def checkpoint(identity: str) -> None:
                await self.reviews.bind_backend_session(claim.token, identity)

            execution = self.execution_factory(claim)
            exited = False
            running = asyncio.create_task(
                execution.run(context, session.backend_session_id, checkpoint)
            )
            try:
                async with asyncio.timeout(context.budget.timeout_s):
                    while not running.done():
                        await asyncio.wait((running,), timeout=self.poll_interval)
                        if not await self.reviews.claim_is_current(claim.token):
                            break
                    if not running.done():
                        output = None
                    else:
                        output = await running
            finally:

                async def stop() -> None:
                    await execution.stop_and_wait()
                    # Exit is established; settle the coroutine as well.
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)

                _, cancelled = await _settle(asyncio.create_task(stop()))
                exited = True
                if cancelled:
                    raise asyncio.CancelledError
            if output is None:
                return await self.reviews.release_review(claim.token)
            return await self.reviews.finalize_review(
                claim.token,
                output,
                diff_index=context.diff_index,
                output_format=self.output_format,
                response_format=self.response_format,
                min_confidence=self.min_confidence,
                findings_threshold=self.findings_threshold,
            )
        except BaseException:
            if exited:
                # An uncertain finalization commit may already have consumed this
                # token. Never retry finalization or overwrite the retained result.
                with suppress(ReviewConflict):
                    await _settle(asyncio.create_task(self.reviews.release_review(claim.token)))
            raise
