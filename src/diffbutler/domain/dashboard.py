"""Local workflow and historical cycle values, without rendering or I/O."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from diffbutler.domain.forge import Account, PullRequest
from diffbutler.domain.review import RunRecord, SessionRecord
from diffbutler.domain.sync import CommentEvent, ReviewEvent


@dataclass(frozen=True)
class CycleRecord:
    task_id: UUID
    account: Account
    pull_request: PullRequest
    cycle: int
    state: str
    completed_at: datetime | None
    completion_reason: str | None
    completed_head_sha: str | None
    cleanup_status: str
    cleanup_error: str | None
    opened_at: datetime | None
    runs: tuple[RunRecord, ...]
    sessions: tuple[SessionRecord, ...]
    receipts: tuple[ReviewEvent | CommentEvent, ...]


COLUMN_IDS = (
    "state",
    "attention",
    "repo",
    "number",
    "title",
    "author",
    "ai_status",
    "findings",
    "max_severity",
    "age",
    "updated",
    "branch",
    "draft",
    "reviewers",
    "unverified",
    "url",
    "additions",
    "deletions",
    "files",
    "labels",
    "ci",
)

STATE_LABELS = {
    "PENDING_AI_REVIEW": "Pending AI review",
    "QUEUED": "AI queued",
    "AI_RUNNING": "AI running",
    "PENDING_HUMAN_REVIEW": "Pending human review",
    "FINDINGS_TRIAGE": "Findings triage",
    "PENDING_AUTHOR_CHANGES": "Pending author changes",
    "DONE": "Completed",
}
