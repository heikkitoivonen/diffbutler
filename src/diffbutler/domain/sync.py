"""Durable monitoring values and pure review-cycle reconciliation."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

from diffbutler.domain.forge import Account, PullRequest


@dataclass(frozen=True)
class RequestEvent:
    id: str
    created_at: datetime

    @property
    def key(self) -> str:
        return f"{self.created_at.astimezone(UTC).isoformat(timespec='microseconds')}:{self.id}"


@dataclass(frozen=True)
class ReviewEvent:
    id: str
    created_at: datetime
    author: str
    state: str
    head_sha: str

    @property
    def key(self) -> str:
        return f"{self.created_at.astimezone(UTC).isoformat(timespec='microseconds')}:{self.id}"


@dataclass(frozen=True)
class ReviewActivity:
    """Requests addressed to the authenticated account or its visible teams."""

    account: Account
    requests: tuple[RequestEvent, ...] = ()
    reviews: tuple[ReviewEvent, ...] = ()
    complete: bool = True
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrackedPR:
    pull_request: PullRequest
    following: bool = True
    sources: tuple[str, ...] = ()
    cycle: int = 1
    task_id: UUID | None = None
    state: str = "PENDING_AI_REVIEW"
    request_key: str = ""
    review_key: str = ""
    completion_reason: str | None = None
    completion_event: str | None = None


@dataclass(frozen=True)
class QueueSnapshot:
    version: int
    items: tuple[TrackedPR, ...]


def reconcile(
    previous: TrackedPR | None,
    pr: PullRequest,
    activity: ReviewActivity,
    sources: tuple[str, ...],
) -> TrackedPR:
    """Observe only complete activity; never dispatch AI or trust posting claims."""
    current = previous or TrackedPR(pr)
    request = max(activity.requests, key=lambda event: event.key, default=None)
    own_reviews = [
        event
        for event in activity.reviews
        if event.author.lower() == activity.account.login.lower()
        and event.state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
    ]
    review = max(own_reviews, key=lambda event: event.key, default=None)
    request_key = request.key if request and activity.complete else current.request_key
    review_key = review.key if review and activity.complete else current.review_key
    # Watermarks never move backwards when access or remote history changes.
    request_key = max(current.request_key, request_key)
    review_key = max(current.review_key, review_key)
    changed = previous is not None and (
        pr.head_sha != previous.pull_request.head_sha or request_key > previous.request_key
    )
    result = replace(
        current, pull_request=pr, sources=sources, request_key=request_key, review_key=review_key
    )
    if changed and pr.state == "open":
        result = replace(
            result,
            state="PENDING_AI_REVIEW",
            completion_reason=None,
            completion_event=None,
            cycle=current.cycle + (current.state == "DONE"),
            task_id=None if current.state == "DONE" else current.task_id,
        )
    approval = (
        activity.complete
        and review is not None
        and review.state == "APPROVED"
        and review.head_sha == pr.head_sha
        and (previous is None or review.key > previous.review_key)
        and (request is None or review.created_at > request.created_at)
    )
    if result.state != "DONE" and (pr.state != "open" or approval):
        result = replace(
            result,
            state="DONE",
            completion_reason=pr.state if pr.state != "open" else "own_approval",
            completion_event=review.key if approval and review else pr.state,
        )
    return result
