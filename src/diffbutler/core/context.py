"""Internal context assembly; does not authorize or launch a reviewer process."""

import asyncio

from diffbutler.domain.forge import PullRequest
from diffbutler.domain.review import ReviewConflict, RunRecord, RunStatus
from diffbutler.forges.base import DiffIndex
from diffbutler.reviewers.base import ReviewBudget, ReviewContext, ReviewSources
from diffbutler.reviewers.redaction import RedactingSourceReader, redact_and_report


async def assemble_context(
    *,
    run: RunRecord,
    pr: PullRequest,
    sources: ReviewSources,
    instructions: str,
    schema_json: str,
    budget: ReviewBudget,
) -> ReviewContext:
    """Assemble local mediated context for an already claimed run.

    The caller must obtain the PR for this task and recheck durable ownership and
    current policy before execution. This snapshot check is not a worker lease or
    an isolation boundary. No source is sent to a model by this function.
    """
    if run.status != RunStatus.RUNNING or pr.state != "open":
        raise ReviewConflict("Context requires a running attempt and an open pull request")
    expected = (run.base_sha, run.head_sha)
    if (pr.base_sha, pr.head_sha) != expected or (sources.base_sha, sources.head_sha) != expected:
        raise ReviewConflict("Review context revisions do not match the requested analysis")
    async with asyncio.timeout(budget.timeout_s):
        diff = await sources.read_diff()
    return ReviewContext(
        pr=pr,
        session_id=run.session_id,
        run_id=run.id,
        comparison_base_sha=sources.comparison_base_sha,
        # Anchor identity records the requested PR pair; hunks and BASE reads
        # both come from its resolved merge base, not the target branch tip.
        diff_index=DiffIndex.parse(diff, base_sha=run.base_sha, head_sha=run.head_sha),
        diff=redact_and_report(diff.encode("utf-8")).decode("utf-8"),
        changed_files=sources.changed_files,
        sources=RedactingSourceReader(sources),
        instructions=instructions,
        schema_json=schema_json,
        budget=budget,
        limitations=(
            "Source is available through mediated reads only; no checkout is provided.",
            "Project tests, builds, and application code were not executed.",
            "Source credential patterns are masked heuristically; undetected secrets may remain.",
            *sources.limitations,
        ),
    )
