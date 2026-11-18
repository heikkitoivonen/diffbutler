"""Review statistics computed from observed forge activity, without I/O."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from diffbutler.domain.forge import Account
from diffbutler.domain.sync import CommentEvent, ReviewEvent

PERIODS = (365, 30, 7)
SUBMITTED = frozenset({"APPROVED", "COMMENTED", "CHANGES_REQUESTED", "DISMISSED"})
# A dismissed review has lost its original type, so it is neither a comment nor approval.
COMMENTING = frozenset({"COMMENTED", "CHANGES_REQUESTED"})


@dataclass(frozen=True)
class PRActivity:
    """Observed reviews and comments on one PR, by every participant."""

    key: str
    author: str
    events: tuple[ReviewEvent | CommentEvent, ...]


@dataclass(frozen=True)
class AuthorStats:
    author: str
    prs_reviewed: int
    reviews: int
    prs_approved: int
    rounds_to_approval: float | None


@dataclass(frozen=True)
class PeriodStats:
    days: int
    covered_days: float
    prs_commented: int
    prs_approved: int
    prs_reviewed: int
    reviews: int
    average_rounds: float | None
    average_rounds_to_approval: float | None
    authors: tuple[AuthorStats, ...]


@dataclass(frozen=True)
class AccountStats:
    account: Account
    since: datetime | None
    periods: tuple[PeriodStats, ...]


@dataclass
class _Author:
    name: str
    prs: int = 0
    reviews: int = 0
    approved: int = 0
    rounds: list[int] = field(default_factory=list[int])


def _mean(values: list[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _rounds(reviews: list[ReviewEvent]) -> int:
    """Each distinct reviewed head commit is one review round."""
    return len({review.head_sha for review in reviews})


def _period(
    days: int, login: str, prs: tuple[PRActivity, ...], now: datetime, since: datetime | None
) -> PeriodStats:
    if since is None:
        return PeriodStats(days, 0.0, 0, 0, 0, 0, None, None, ())
    start = max(now - timedelta(days=days), since)
    # Count at least one day so a just-started install does not inflate averages.
    covered = max(1.0, (now - start) / timedelta(days=1))
    me = login.casefold()
    commented = approved = reviewed = review_count = 0
    rounds: list[int] = []
    to_approval: list[int] = []
    authors: dict[str, _Author] = {}
    for pr in prs:
        if pr.author.casefold() == me:
            continue
        reviews = [
            event
            for event in pr.events
            if isinstance(event, ReviewEvent)
            and event.state in SUBMITTED
            and event.created_at <= now
        ]
        submitted = {event.id for event in reviews}
        mine = sorted(
            (event for event in reviews if event.author.casefold() == me),
            key=lambda event: (event.created_at, event.id),
        )
        in_period = [event for event in mine if event.created_at >= start]
        # Inline comments belong to a review; skip any whose review was never submitted.
        comments = [
            event
            for event in pr.events
            if isinstance(event, CommentEvent)
            and event.author.casefold() == me
            and start <= event.created_at <= now
            and (not event.id.startswith("inline:") or event.review_id in submitted)
        ]
        if comments or any(event.state in COMMENTING for event in in_period):
            commented += 1
        if not in_period:
            continue
        author = authors.setdefault(pr.author.casefold(), _Author(pr.author))
        reviewed += 1
        review_count += len(in_period)
        rounds.append(_rounds(in_period))
        author.prs += 1
        author.reviews += len(in_period)
        approval = next((event for event in in_period if event.state == "APPROVED"), None)
        if approval is not None:
            approved += 1
            author.approved += 1
            # Rounds before the period still count toward reaching this approval.
            needed = _rounds(mine[: mine.index(approval) + 1])
            to_approval.append(needed)
            author.rounds.append(needed)
    ranked = sorted(
        authors.values(), key=lambda item: (-item.prs, -item.reviews, item.name.casefold())
    )
    return PeriodStats(
        days,
        covered,
        commented,
        approved,
        reviewed,
        review_count,
        _mean(rounds),
        _mean(to_approval),
        tuple(
            AuthorStats(item.name, item.prs, item.reviews, item.approved, _mean(item.rounds))
            for item in ranked
        ),
    )


def review_statistics(
    account: Account,
    prs: tuple[PRActivity, ...],
    now: datetime,
    since: datetime | None,
    periods: tuple[int, ...] = PERIODS,
) -> AccountStats:
    """Summarize one account's reviews over trailing periods ending at ``now``.

    ``since`` is when local tracking began; periods start no earlier, because older
    activity was only observed on PRs that were tracked later.
    """
    return AccountStats(
        account,
        since,
        tuple(_period(days, account.login, prs, now, since) for days in periods),
    )
