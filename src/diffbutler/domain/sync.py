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
    body: str = ""

    @property
    def key(self) -> str:
        return f"{self.created_at.astimezone(UTC).isoformat(timespec='microseconds')}:{self.id}"


@dataclass(frozen=True)
class CommentEvent:
    id: str
    created_at: datetime
    author: str
    body: str
    head_sha: str = ""
    review_id: str | None = None


@dataclass(frozen=True)
class ReviewActivity:
    """Requests addressed to the authenticated account or its visible teams."""

    account: Account
    requests: tuple[RequestEvent, ...] = ()
    reviews: tuple[ReviewEvent, ...] = ()
    complete: bool = True
    warnings: tuple[str, ...] = ()
    comments: tuple[CommentEvent, ...] = ()


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
    activity: ReviewActivity | None = None
    refresh_pending: bool = False
    manual_after: str = ""
    opened_at: datetime | None = None
    notice: str = ""
    posted_at: datetime | None = None
    review_requested_at: datetime | None = None


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
        and event.state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED", "COMMENTED"}
    ]
    review = max(own_reviews, key=lambda event: event.key, default=None)
    request_key = request.key if request and activity.complete else current.request_key
    review_key = review.key if review and activity.complete else current.review_key
    # Watermarks never move backwards when access or remote history changes.
    request_key = max(current.request_key, request_key)
    review_key = max(current.review_key, review_key)
    head_changed = previous is not None and pr.head_sha != previous.pull_request.head_sha
    requested_again = (
        previous is not None
        and request_key > previous.request_key
        and request is not None
        and (
            current.review_requested_at is None or request.created_at > current.review_requested_at
        )
    )
    changed = head_changed or requested_again
    result = replace(
        current,
        pull_request=pr,
        sources=sources,
        request_key=request_key,
        review_key=review_key,
        activity=activity if activity.complete else current.activity,
        refresh_pending=not activity.complete,
    )
    if changed and pr.state == "open":
        changed_after_approval = (
            head_changed
            and not requested_again
            and (
                current.state == "DONE_PLUS"
                or (
                    current.state == "DONE"
                    and current.completion_reason in {"own_approval", "local_approval"}
                )
            )
        )
        result = replace(
            result,
            state="DONE_PLUS" if changed_after_approval else "PENDING_AI_REVIEW",
            completion_reason=None,
            completion_event=None,
            cycle=current.cycle + (current.state == "DONE"),
            task_id=None if current.state == "DONE" else current.task_id,
            notice="Review requested again" if requested_again else "New commits detected",
            posted_at=None if current.state == "DONE" else current.posted_at,
        )
    feedback = (
        previous is not None
        and current.activity is not None
        and not changed
        and activity.complete
        and review is not None
        and review.state in {"COMMENTED", "CHANGES_REQUESTED"}
        and review.head_sha == pr.head_sha
        and review.key > max(current.review_key, current.manual_after)
        and (request is None or review.created_at > request.created_at)
    )
    if result.state != "DONE" and feedback:
        count = (
            sum(comment.review_id == review.id for comment in activity.comments) if review else 0
        )
        result = replace(
            result,
            state="PENDING_AUTHOR_CHANGES",
            notice=f"Posted review detected · {count} comments",
            posted_at=review.created_at if review else current.posted_at,
        )
    elif not changed and activity.complete and current.activity is not None:
        known = {comment.id for comment in current.activity.comments}
        count = sum(comment.id not in known for comment in activity.comments)
        if count:
            result = replace(result, notice=f"New discussion activity · {count} comments")
        else:
            known_reviews = {event.id for event in current.activity.reviews}
            new_reviews = sum(event.id not in known_reviews for event in activity.reviews)
            if new_reviews:
                result = replace(result, notice=f"New review activity · {new_reviews} reviews")
    decisive = max(
        (event for event in own_reviews if event.state != "COMMENTED"),
        key=lambda event: event.key,
        default=None,
    )
    approval = (
        activity.complete
        and decisive is not None
        and decisive.state == "APPROVED"
        and decisive.head_sha == pr.head_sha
        and (previous is None or decisive.key > previous.review_key)
        and (request is None or decisive.created_at > request.created_at)
    )
    if result.state != "DONE" and (pr.state != "open" or approval):
        result = replace(
            result,
            state="DONE",
            completion_reason=pr.state if pr.state != "open" else "own_approval",
            completion_event=decisive.key if approval and decisive else pr.state,
        )
    return result
