"""Human-readable reviews retain prose without treating it as structured findings."""

import json
from pathlib import Path

import pytest
from tests.test_review_claims import seed, service
from tests.test_sync import ACCOUNT

from diffbutler.reviewers.base import ReviewOutput
from diffbutler.reviewers.extraction import StreamFormat
from diffbutler.reviewers.parsing import parse_review
from diffbutler.store.database import Database
from diffbutler.store.queue import SQLiteQueueStore

DRAFT = (
    "## Expired sessions remain valid\n\n"
    "Location: `src/session.py:42` (head).\n\n"
    "Draft comment:\nPlease reject expired sessions before returning the user. "
    "Otherwise, a stolen expired token still grants access.\n\n"
    "Limitations: Project checks were not executed.\n"
)


@pytest.mark.parametrize("complete", [False, True])
def test_drafts_require_complete_backend_output_and_ignore_tool_text(complete: bool) -> None:
    events = [
        {"type": "session", "id": "saved-session"},
        {"type": "assistant", "text": "Earlier incomplete analysis"},
        {"type": "assistant", "text": DRAFT},
        {"type": "tool", "text": "No issues found"},
    ]
    if complete:
        events.append({"type": "complete"})
    output = ReviewOutput("\n".join(json.dumps(event) for event in events).encode(), b"", 0)
    parsed = parse_review(output, output_format=StreamFormat(), response_format="markdown")
    assert parsed.output.stdout == output.stdout
    assert parsed.document is None
    if complete:
        assert parsed.error is None and parsed.draft is not None
        assert parsed.draft.markdown == DRAFT
        assert parsed.output.backend_session_id == "saved-session"
    else:
        assert parsed.error is not None and parsed.draft is None


@pytest.mark.parametrize(
    ("text", "exit_code", "budget", "error"),
    [
        (b" \n", 0, 100, "empty review response"),
        (b"\xff", 0, 100, "output is not UTF-8"),
        (b"No issues found", 1, 100, "reviewer exited unsuccessfully"),
        (b"No issues found", 0, 2, "output exceeds byte budget"),
    ],
)
def test_invalid_drafts_are_not_successful_reviews(
    text: bytes, exit_code: int, budget: int, error: str
) -> None:
    parsed = parse_review(
        ReviewOutput(text, b"", exit_code), response_format="markdown", max_output_bytes=budget
    )
    assert parsed.error == error and parsed.draft is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [DRAFT, "No issues found. Project checks were not executed."])
async def test_drafts_survive_restart_and_always_await_human_review(
    tmp_path: Path, text: str
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/drafts.db"
    database = Database(url)
    await database.initialize()
    try:
        _, identity = await seed(database)
        reviews = service(database)
        run = await reviews.request_review(identity)
        claim = await reviews.claim_review(run.id)
        assert claim is not None
        finished = await reviews.finalize_review(
            claim.token, ReviewOutput(text.encode(), b"", 0), response_format="markdown"
        )
        assert finished.status == "succeeded"
    finally:
        await database.close()
    reopened = Database(url)
    try:
        result = await service(reopened).get_result(run.id)
        assert result is not None and result.markdown == text
        assert result.document is None and result.error is None
        assert result.selected_indices == () and result.anchors is None
        assert result.parse_tier == "markdown"
        snapshot = await SQLiteQueueStore(reopened).snapshot(ACCOUNT)
        assert snapshot.items[0].state == "PENDING_HUMAN_REVIEW"
    finally:
        await reopened.close()
