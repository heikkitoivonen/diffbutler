"""Review statistics from locally observed activity, computed and shown offline."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import update
from tests.test_sync import ACCOUNT, PR, FakeForge
from textual.widgets import Static
from typer.testing import CliRunner

from diffbutler.config.settings import load_settings
from diffbutler.core.dashboard import statistics_configured
from diffbutler.core.sync import DiffButler
from diffbutler.domain.stats import AccountStats, AuthorStats, PRActivity, review_statistics
from diffbutler.domain.sync import CommentEvent, ReviewActivity, ReviewEvent
from diffbutler.store.database import Database
from diffbutler.store.models import ReviewTask
from diffbutler.store.queue import SQLiteQueueStore
from diffbutler.ui.cli import app as cli
from diffbutler.ui.statistics import format_statistics
from diffbutler.ui.tui.dashboard import Dashboard, DetailScreen

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


def review(id: str, days: float, state: str, head: str, author: str = "reader") -> ReviewEvent:
    return ReviewEvent(f"review:{id}", NOW - timedelta(days=days), author, state, head)


def comment(
    id: str, days: float, author: str = "reader", review: str | None = None
) -> CommentEvent:
    return CommentEvent(id, NOW - timedelta(days=days), author, "Note", "h1", review)


PRS = (
    PRActivity(
        "acme/api#1",
        "alice",
        (
            review("a1", 2, "COMMENTED", "h1"),
            review("a2", 1, "APPROVED", "h2", author="Reader"),
            review("other", 1, "APPROVED", "h2", author="someone"),
        ),
    ),
    PRActivity("acme/api#2", "Alice", (review("b1", 20, "APPROVED", "h1"),)),
    PRActivity(
        "acme/api#3",
        "bob",
        (
            review("c1", 100, "CHANGES_REQUESTED", "h1"),
            review("c2", 99, "COMMENTED", "h1"),
            review("c3", 5, "APPROVED", "h2"),
        ),
    ),
    PRActivity("acme/api#4", "carol", (comment("discussion:1", 3),)),
    # Your own PRs and comments from unsubmitted reviews are not review activity.
    PRActivity(
        "acme/api#5",
        "READER",
        (comment("discussion:2", 1), review("e1", 1, "COMMENTED", "h1")),
    ),
    PRActivity("acme/api#6", "dave", (comment("inline:1", 1, review="review:pending"),)),
    PRActivity("acme/api#7", "erin", (review("f1", -1, "APPROVED", "h1"),)),
)


def test_periods_count_reviews_comments_approvals_and_rounds() -> None:
    stats = review_statistics(ACCOUNT, PRS, NOW, NOW - timedelta(days=400))
    year, month, week = stats.periods
    assert [period.days for period in stats.periods] == [365, 30, 7]
    assert [period.covered_days for period in stats.periods] == [365, 30, 7]

    assert (week.prs_commented, week.prs_approved, week.prs_reviewed, week.reviews) == (2, 2, 2, 3)
    assert week.average_rounds == 1.5
    # PR 3 took two reviewed commits to approve, one of them before this period.
    assert week.average_rounds_to_approval == 2
    assert week.authors == (AuthorStats("alice", 1, 2, 1, 2), AuthorStats("bob", 1, 1, 1, 2))

    assert (month.prs_commented, month.prs_approved, month.prs_reviewed, month.reviews) == (
        2,
        3,
        3,
        4,
    )
    assert month.average_rounds == pytest.approx(4 / 3)
    assert month.average_rounds_to_approval == pytest.approx(5 / 3)
    assert month.authors == (AuthorStats("alice", 2, 3, 2, 1.5), AuthorStats("bob", 1, 1, 1, 2))

    # Two reviews on the same commit are one round.
    assert (year.prs_commented, year.prs_approved, year.prs_reviewed, year.reviews) == (3, 3, 3, 6)
    assert year.average_rounds == pytest.approx(5 / 3)
    assert year.authors == (AuthorStats("alice", 2, 3, 2, 1.5), AuthorStats("bob", 1, 3, 1, 2))


def test_dismissed_ties_and_unsubmitted_reviews() -> None:
    later = NOW - timedelta(days=1)
    prs = (
        # A dismissed review may have been an approval; it is neither kind.
        PRActivity("acme/api#1", "gina", (review("dismissed", 1, "DISMISSED", "h1"),)),
        # A review at the approval's timestamp but ordered after it is a later round.
        PRActivity(
            "acme/api#2",
            "frank",
            (
                ReviewEvent("review:x1", later, "reader", "APPROVED", "h1"),
                ReviewEvent("review:x2", later, "reader", "COMMENTED", "h2"),
            ),
        ),
        # An inline comment whose review is submitted after now is still pending.
        PRActivity(
            "acme/api#3",
            "hank",
            (
                review("late", -1, "COMMENTED", "h1"),
                comment("inline:1", 1, review="review:late"),
            ),
        ),
    )
    week = review_statistics(ACCOUNT, prs, NOW, NOW - timedelta(days=30)).periods[-1]
    assert (week.prs_commented, week.prs_approved, week.prs_reviewed, week.reviews) == (1, 1, 2, 3)
    assert week.average_rounds_to_approval == 1
    assert week.authors == (AuthorStats("frank", 1, 2, 1, 1), AuthorStats("gina", 1, 1, 0, None))


def test_periods_start_no_earlier_than_tracking() -> None:
    stats = review_statistics(ACCOUNT, PRS, NOW, NOW - timedelta(days=10))
    assert [period.covered_days for period in stats.periods] == [10, 10, 7]
    year = stats.periods[0]
    assert (year.prs_approved, year.prs_reviewed, year.reviews) == (2, 2, 3)
    assert year.average_rounds_to_approval == 2

    recent = review_statistics(ACCOUNT, PRS, NOW, NOW - timedelta(hours=1))
    assert [period.covered_days for period in recent.periods] == [1, 1, 1]

    untracked = review_statistics(ACCOUNT, PRS, NOW, None)
    assert all(period.covered_days == 0 and not period.authors for period in untracked.periods)


def test_format_shows_averages_and_top_authors() -> None:
    text = format_statistics((review_statistics(ACCOUNT, PRS, NOW, NOW - timedelta(days=400)),))
    lines = text.splitlines()
    assert lines[0] == f"Review statistics · reader ({ACCOUNT.forge})"
    assert "Local observations since 2025-09-27" in lines[1]
    assert lines[3].split() == ["365", "days", "30", "days", "7", "days"]
    assert lines[6].split() == ["per", "day", "0.01", "0.07", "0.29"]
    assert lines[13].split() == ["Average", "rounds", "to", "approval", "1.67", "1.67", "2.00"]
    assert "Last 7 days by PR author" in lines
    assert text.count("Most reviewed for: alice (2 PRs)") == 2
    assert "Most reviewed for: alice (1 PR)" in lines
    # Equal rounds and approvals fall back to the name.
    assert text.count("Most rounds to approval: bob (2.00)") == 2
    assert "Most rounds to approval: alice (2.00)" in lines

    assert "No forge accounts" in format_statistics(())
    empty = format_statistics((AccountStats(ACCOUNT, None, ()),))
    assert empty.endswith("No PRs have been tracked yet.")
    quiet = format_statistics((review_statistics(ACCOUNT, (), NOW, NOW - timedelta(days=1)),))
    assert quiet.count("No reviews in this period.") == 3


@pytest_asyncio.fixture
async def observed(tmp_path: Path) -> AsyncGenerator[Path]:
    """A synced queue whose tracking began two days ago, with a day-old approval."""
    path = tmp_path / "stats.toml"
    path.write_text(f'[db]\nurl = "sqlite+aiosqlite:///{tmp_path}/stats.db"\n')
    database = Database(load_settings(path).db.url)
    await database.initialize()
    try:
        forge = FakeForge()
        queue = SQLiteQueueStore(database)
        await DiffButler(queue, forge).sync()
        now = datetime.now(UTC)
        async with database.sessions() as db:
            await db.execute(update(ReviewTask).values(first_seen_at=now - timedelta(days=2)))
            await db.commit()
        day = now - timedelta(days=1)
        forge.activity = ReviewActivity(
            ACCOUNT,
            reviews=(
                ReviewEvent("review:1", day, "reader", "COMMENTED", "old-head"),
                ReviewEvent("review:2", day + timedelta(hours=1), "reader", "APPROVED", "head"),
            ),
            comments=(CommentEvent("discussion:1", day, "reader", "Looks close"),),
        )
        forge.prs[PR.ref] = replace(PR, updated_at=now)
        await DiffButler(queue, forge).sync()
        yield path
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_statistics_use_retained_activity(observed: Path) -> None:
    (stats,) = await statistics_configured(observed)
    assert stats.account.login == "reader"
    for period in stats.periods:
        assert period.covered_days == pytest.approx(2, abs=0.01)
        assert (period.prs_commented, period.prs_approved, period.reviews) == (1, 1, 2)
        assert period.average_rounds_to_approval == 2
        assert period.authors == (AuthorStats(PR.author, 1, 2, 1, 2),)
    assert await statistics_configured(observed, login="someone-else") == ()


@pytest.mark.serial
@pytest.mark.asyncio
async def test_t_opens_statistics_and_cli_prints_them(observed: Path) -> None:
    expected = format_statistics(await statistics_configured(observed))
    app = Dashboard(observed, load_settings(observed), offline=True)
    async with app.run_test() as pilot:
        await pilot.press("t")
        await asyncio.wait_for(asyncio.gather(*(worker.wait() for worker in app.workers)), 5)
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert str(app.screen.query_one(Static).content) == expected
        await pilot.press("escape")
        assert not isinstance(app.screen, DetailScreen)

    result = await asyncio.to_thread(CliRunner().invoke, cli, ["stats", "--config", str(observed)])
    assert result.exit_code == 0, result.output
    assert result.output == expected + "\n"
    assert "Reviews submitted" in result.output and "PRs approved" in result.output
    result = await asyncio.to_thread(
        CliRunner().invoke, cli, ["stats", "--config", str(observed), "--login", "nobody"]
    )
    assert result.exit_code == 2 and "No stored forge account has login" in result.output
