"""Plain-text review statistics shared by the CLI and the dashboard."""

from diffbutler.domain.stats import AccountStats, AuthorStats, PeriodStats


def _number(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _days(value: float) -> str:
    return f"{value:.1f}".removesuffix(".0")


def _per_day(count: int, period: PeriodStats) -> str:
    return _number(count / period.covered_days if period.covered_days else None)


def _table(rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    return [
        "  ".join(
            cell.ljust(widths[index]) if index == 0 else cell.rjust(widths[index])
            for index, cell in enumerate(row)
        ).rstrip()
        for row in rows
    ]


def _authors(period: PeriodStats) -> list[str]:
    lines = [f"Last {period.days} days by PR author"]
    if not period.authors:
        return [*lines, "No reviews in this period."]
    lines += _table(
        [
            ("Author", "PRs reviewed", "Reviews", "Approved", "Rounds to approval"),
            *(
                (
                    item.author,
                    str(item.prs_reviewed),
                    str(item.reviews),
                    str(item.prs_approved),
                    _number(item.rounds_to_approval),
                )
                for item in period.authors
            ),
        ]
    )
    top = period.authors[0]
    plural = "" if top.prs_reviewed == 1 else "s"
    lines.append(f"Most reviewed for: {top.author} ({top.prs_reviewed} PR{plural})")
    rounds = [item for item in period.authors if item.rounds_to_approval is not None]
    if rounds:

        def needs(item: AuthorStats) -> tuple[float, int, str]:
            assert item.rounds_to_approval is not None
            return (-item.rounds_to_approval, -item.prs_approved, item.author.casefold())

        most = min(rounds, key=needs)
        lines.append(f"Most rounds to approval: {most.author} ({_number(most.rounds_to_approval)})")
    return lines


def format_statistics(accounts: tuple[AccountStats, ...]) -> str:
    if not accounts:
        return "No forge accounts are stored yet; sync once to start collecting statistics."
    sections: list[str] = []
    for stats in accounts:
        lines = [f"Review statistics · {stats.account.login} ({stats.account.forge})"]
        if stats.since is None:
            sections.append("\n".join([*lines, "No PRs have been tracked yet."]))
            continue
        lines += [
            f"Local observations since {stats.since:%Y-%m-%d}; activity on PRs DiffButler "
            "did not track is not counted.",
            "",
        ]
        periods = stats.periods
        lines += _table(
            [
                ("", *(f"{period.days} days" for period in periods)),
                ("Days covered", *(_days(period.covered_days) for period in periods)),
                ("PRs commented on", *(str(period.prs_commented) for period in periods)),
                ("  per day", *(_per_day(period.prs_commented, period) for period in periods)),
                ("PRs approved", *(str(period.prs_approved) for period in periods)),
                ("  per day", *(_per_day(period.prs_approved, period) for period in periods)),
                ("Reviews submitted", *(str(period.reviews) for period in periods)),
                ("  per day", *(_per_day(period.reviews, period) for period in periods)),
                ("PRs reviewed", *(str(period.prs_reviewed) for period in periods)),
                (
                    "Average rounds per PR",
                    *(_number(period.average_rounds) for period in periods),
                ),
                (
                    "Average rounds to approval",
                    *(_number(period.average_rounds_to_approval) for period in periods),
                ),
            ]
        )
        for period in periods:
            lines += ["", *_authors(period)]
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
