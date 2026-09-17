"""Context assembly uses pinned offline sources and never invokes a reviewer."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from diffbutler.core.context import assemble_context
from diffbutler.domain.forge import PRRef, PullRequest
from diffbutler.domain.review import (
    ChangedFile,
    ReviewConflict,
    RunRecord,
    RunStatus,
    Side,
    SourceFile,
)
from diffbutler.reviewers.base import ReviewBudget


class FakeSources:
    base_sha = "base"
    head_sha = "head"
    changed_files = (ChangedFile("changed.py"),)
    limitations = ("Synthetic source limitation.",)

    def __init__(self, diff: str = "synthetic diff") -> None:
        self.diff = diff
        self.requested_limits: list[int] = []
        self.started = asyncio.Event()
        self.block = False
        self.cancelled = False

    async def read_diff(self, *, max_bytes: int) -> str:
        self.requested_limits.append(max_bytes)
        self.started.set()
        if self.block:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
        return self.diff

    async def read_file(self, source: SourceFile) -> bytes | None:
        return b"surrounding source" if source.path == "unchanged.py" else None


def records() -> tuple[RunRecord, PullRequest]:
    now = datetime.now(UTC)
    run = RunRecord(
        uuid4(),
        uuid4(),
        uuid4(),
        "fake",
        "head",
        "base",
        "v1",
        "config",
        "key",
        1,
        RunStatus.RUNNING,
        now,
        now,
        None,
    )
    pr = PullRequest(
        PRRef("example", "acme", "api", 1),
        "Synthetic",
        "author",
        "https://example.invalid/1",
        "open",
        "head",
        "base",
        "topic",
        "main",
        False,
        now,
        now,
        (),
        (),
    )
    return run, pr


@pytest.mark.asyncio
async def test_context_preserves_identity_and_surrounding_source() -> None:
    run, pr = records()
    sources = FakeSources()
    budget = ReviewBudget(max_diff_bytes=100)
    context = await assemble_context(
        run=run,
        pr=pr,
        sources=sources,
        instructions="trusted instructions",
        schema_json='{"type":"object"}',
        budget=budget,
    )
    assert context.run_id == run.id and context.session_id == run.session_id
    assert context.pr is pr and context.budget is budget
    assert context.diff == "synthetic diff" and context.changed_files == sources.changed_files
    assert context.instructions == "trusted instructions"
    assert context.schema_json == '{"type":"object"}'
    assert context.checkout_path is None
    assert context.limitations[-1] == sources.limitations[0]
    assert sources.requested_limits == [100]
    assert (
        await context.sources.read_file(SourceFile("unchanged.py", Side.HEAD))
        == b"surrounding source"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch", ["run", "closed", "pr_base", "pr_head", "source_base", "source_head"]
)
async def test_invalid_context_rejected_before_source_io(mismatch: str) -> None:
    run, pr = records()
    sources = FakeSources()
    if mismatch == "run":
        run = replace(run, status=RunStatus.QUEUED)
    elif mismatch == "closed":
        pr = replace(pr, state="closed")
    elif mismatch == "pr_base":
        pr = replace(pr, base_sha="moved")
    elif mismatch == "pr_head":
        pr = replace(pr, head_sha="moved")
    elif mismatch == "source_base":
        sources.base_sha = "wrong"
    else:
        sources.head_sha = "wrong"
    with pytest.raises(ReviewConflict):
        await assemble_context(
            run=run,
            pr=pr,
            sources=sources,
            instructions="",
            schema_json="{}",
            budget=ReviewBudget(),
        )
    assert sources.requested_limits == []


@pytest.mark.asyncio
async def test_diff_budget_counts_bytes_without_silent_truncation() -> None:
    run, pr = records()
    with pytest.raises(ValueError, match="byte limit"):
        await assemble_context(
            run=run,
            pr=pr,
            sources=FakeSources("éé"),
            instructions="",
            schema_json="{}",
            budget=ReviewBudget(max_diff_bytes=3),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_assembly_timeout_and_cancellation_propagate(cancel: bool) -> None:
    run, pr = records()
    sources = FakeSources()
    sources.block = True
    task = asyncio.create_task(
        assemble_context(
            run=run,
            pr=pr,
            sources=sources,
            instructions="",
            schema_json="{}",
            budget=ReviewBudget(timeout_s=1),
        )
    )
    await sources.started.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(TimeoutError):
            await task
    assert sources.cancelled
