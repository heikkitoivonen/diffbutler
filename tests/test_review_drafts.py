"""Human-readable reviews retain prose without treating it as structured findings."""

import json
from pathlib import Path

import pytest
from tests.test_review_claims import seed, service
from tests.test_sync import ACCOUNT

from diffbutler.core.sync import DiffButler
from diffbutler.domain.dashboard import STATE_LABELS
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


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["malformed", "truncated", "empty"])
async def test_unreadable_backend_output_needs_check_after_restart(
    tmp_path: Path, kind: str
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/unreadable.db")
    await database.initialize()
    try:
        forge, identity = await seed(database)
        reviews = service(database)
        run = await reviews.request_review(identity)
        claim = await reviews.claim_review(run.id)
        assert claim is not None
        await reviews.bind_backend_session(claim.token, "saved-session")
        events = [
            {"type": "session", "id": "saved-session"},
            {"type": "assistant", "text": "" if kind == "empty" else DRAFT},
        ]
        if kind != "truncated":
            events.append({"type": "complete"})
        raw = "\n".join(json.dumps(event) for event in events).encode()
        if kind == "malformed":
            raw += b"\n{invalid json"
        output = ReviewOutput(raw, b"diagnostic", 0)
        finished = await reviews.finalize_review(
            claim.token, output, output_format=StreamFormat(), response_format="markdown"
        )
        assert finished.status == "failed"
        assert finished.status_reason == "invalid backend output"
    finally:
        await database.close()
    reopened = Database(str(database.url))
    try:
        await reopened.initialize(auto_migrate=False)
        restored = service(reopened)
        result = await restored.get_result(run.id)
        assert result is not None and result.error == "invalid backend output"
        assert result.stdout == raw and result.stderr == output.stderr
        assert result.markdown is None and result.document is None
        assert (await restored.get_session(run.session_id)).backend_session_id == "saved-session"
        queue = SQLiteQueueStore(reopened)
        await DiffButler(queue, forge).sync()
        state = (await queue.snapshot(ACCOUNT)).items[0].state
        assert state == "FINDINGS_TRIAGE" and STATE_LABELS[state] == "check"
        assert (await restored.get_run(run.id)).status == "failed"
        retry = await restored.request_review(identity)
        assert retry.attempt == 2 and retry.session_id == run.session_id
        assert await restored.claim_review(retry.id) is not None
    finally:
        await reopened.close()


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
    ("text", "exit_code", "error"),
    [
        (b" \n", 0, "empty review response"),
        (b"\xff", 0, "output is not UTF-8"),
        (
            b"No issues found",
            1,
            "reviewer exited unsuccessfully (exit code 1); inspect captured output",
        ),
    ],
)
def test_invalid_drafts_are_not_successful_reviews(text: bytes, exit_code: int, error: str) -> None:
    parsed = parse_review(ReviewOutput(text, b"", exit_code), response_format="markdown")
    assert parsed.error == error and parsed.draft is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_state"),
    [
        (DRAFT, "FINDINGS_TRIAGE"),
        ("No issues found. Project checks were not executed.", "FINDINGS_TRIAGE"),
        ("Review outcome: no_findings\n\nNo issues found.", "PENDING_HUMAN_REVIEW"),
        ("Review outcome: findings\n\n" + DRAFT, "FINDINGS_TRIAGE"),
        ("Review outcome: inconclusive\nContext unavailable.", "FINDINGS_TRIAGE"),
        ("Review outcome: unknown\nNo issues found.", "FINDINGS_TRIAGE"),
        ("```\nReview outcome: no_findings\n```", "FINDINGS_TRIAGE"),
        ("Review outcome: no_findings\nReview outcome: findings", "FINDINGS_TRIAGE"),
        ("Review outcome: no_findings\nReview outcome: no_findings", "FINDINGS_TRIAGE"),
        pytest.param("Review notes.\n" * 310_000, "FINDINGS_TRIAGE", id="large-output"),
    ],
)
async def test_draft_outcomes_survive_restart(
    tmp_path: Path, text: str, expected_state: str
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
        assert snapshot.items[0].state == expected_state
        assert STATE_LABELS[expected_state] == (
            "approve" if expected_state == "PENDING_HUMAN_REVIEW" else "check"
        )
    finally:
        await reopened.close()
