"""Typer interface to the shared core; terminal rendering stays here."""

import asyncio
import json
import sys
from collections.abc import Coroutine, Sequence
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import TypeAdapter
from sqlalchemy.exc import SQLAlchemyError

from diffbutler.config.settings import ConfigError
from diffbutler.core.application import (
    browse_configured,
    follow_configured,
    local_queue,
    sync_configured,
)
from diffbutler.core.doctor import run_doctor
from diffbutler.core.managed import (
    approved_configured,
    cleanup_configured,
    findings_configured,
    handoff_configured,
    review_configured,
)
from diffbutler.core.native import foreground_environment
from diffbutler.core.sync import PullRequestRow
from diffbutler.domain.forge import PRPage, PRQuery, PullRequest
from diffbutler.domain.lifecycle import CleanupStatus
from diffbutler.domain.review import ReviewConflict
from diffbutler.forges.base import ForgeError
from diffbutler.reviewers.native import NativeError
from diffbutler.sources.git import SourceError
from diffbutler.store.database import DatabaseError

app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
    help="A terminal dashboard for human-directed AI code reviews.",
    epilog="Native CLI reviews are available; the dashboard is planned for M3.",
)
ConfigOption = Annotated[Path | None, typer.Option(help="Explicit TOML file (must exist)")]
ForgeOption = Annotated[int, typer.Option(min=1, help="Configured forge, numbered from one")]


def run[T](operation: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(operation)
    except (
        ConfigError,
        DatabaseError,
        ForgeError,
        ReviewConflict,
        NativeError,
        SourceError,
    ) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    except TimeoutError:
        typer.echo("Error: operation timed out; inspect the saved run status", err=True)
        raise typer.Exit(1) from None
    except OSError, SQLAlchemyError:
        typer.echo(
            "Error: local storage unavailable; check permissions, space, and database integrity",
            err=True,
        )
        raise typer.Exit(1) from None


def show_version(value: bool) -> None:
    if value:
        typer.echo(f"diffbutler {version('diffbutler')}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def root(
    context: typer.Context,
    version_flag: Annotated[
        bool, typer.Option("--version", callback=show_version, is_eager=True)
    ] = False,
) -> None:
    if context.invoked_subcommand is None:
        typer.echo(context.get_help())


@app.command()
def doctor(
    config: ConfigOption = None,
    repo: str | None = None,
    offline: bool = False,
    db_url: str | None = None,
    no_migrate: bool = False,
) -> None:
    """Validate configuration, SQLite, and configured forge access."""
    db: dict[str, object] = {}
    if db_url is not None:
        db["url"] = db_url
    if no_migrate:
        db["auto_migrate"] = False
    report = run(
        run_doctor(config, repo=repo, overrides={"db": db} if db else None, offline=offline)
    )
    for check in report.checks:
        typer.echo(f"{check.status:4}  {check.name:20}  {check.detail}")
    if not report.passed:
        raise typer.Exit(1)


@app.command()
def sync(
    config: ConfigOption = None,
    forge: Annotated[int | None, typer.Option(min=1)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Persist requests and refresh followed PRs. Never starts AI reviews."""
    reports = run(sync_configured(config, forge))
    if json_output:
        typer.echo(json.dumps([report.model_dump(mode="json") for report in reports]))
    else:
        for report in reports:
            typer.echo(
                f"{report.account.login}: {report.discovered} requested, "
                f"{report.refreshed} refreshed, {report.active} active"
            )
            for warning in report.warnings:
                typer.echo(f"Warning: {safe(warning)}", err=True)
    if any(not report.complete for report in reports):
        raise typer.Exit(1)


class View(StrEnum):
    active = "active"
    requested = "requested"
    following = "following"
    all = "all"


class PRState(StrEnum):
    open = "open"
    closed = "closed"
    merged = "merged"
    all = "all"


def safe(value: str) -> str:
    """Do not let forge-controlled strings execute terminal escape sequences."""
    return "".join(character if character.isprintable() else " " for character in value)


def print_pr(pr: PullRequest, state: str = "", account: str = "") -> None:
    ref = pr.ref
    typer.echo(
        safe(
            f"{account} {ref.namespace}/{ref.repository}#{ref.number}  "
            f"{state or pr.state}  {pr.title}  {pr.url}"
        ).strip()
    )


def print_row(row: PullRequestRow) -> None:
    for warning in row.warnings:
        typer.echo(f"Warning: {safe(warning)}", err=True)
    print_pr(row.tracked.pull_request, row.tracked.state, row.account.login)
    if row.tracked.sources:
        typer.echo("  requested: " + safe(", ".join(row.tracked.sources)))


@app.command()
def ls(
    config: ConfigOption = None,
    view: View = View.active,
    forge: Annotated[int | None, typer.Option(min=1)] = None,
    login: str | None = None,
    repo: str | None = None,
    author: str | None = None,
    state: PRState = PRState.open,
    text: str = "",
    cursor: str | None = None,
    page_size: Annotated[int, typer.Option(min=1, max=100)] = 30,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List the local queue, or browse accessible PRs with --view all."""
    if view == View.all:
        if login is not None:
            raise typer.BadParameter("--login filters local accounts; it requires a local view")
        try:
            query = PRQuery(
                repository=repo, author=author, state=state.value, text=text, page_size=page_size
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        page = run(browse_configured(config, forge or 1, query, cursor))
        if json_output:
            typer.echo(TypeAdapter(PRPage).dump_json(page).decode())
        else:
            for pr in page.items:
                print_pr(pr)
            for warning in page.warnings:
                typer.echo(f"Warning: {safe(warning)}", err=True)
            if page.next_cursor:
                typer.echo(f"Next page: --cursor {safe(page.next_cursor)}")
        if not page.complete:
            raise typer.Exit(1)
    else:
        if forge is not None:
            raise typer.BadParameter("--forge selects remote browsing; use --view all")
        if repo or author or text or cursor or state != PRState.open or page_size != 30:
            raise typer.BadParameter("Search filters require --view all")
        rows = run(local_queue(config, view.value, login))
        if json_output:
            typer.echo(json.dumps([row.model_dump(mode="json") for row in rows]))
        else:
            for row in rows:
                print_row(row)


@app.command()
def follow(
    pr: Annotated[str, typer.Argument(help="owner/repo#number or a PR URL")],
    config: ConfigOption = None,
    forge: ForgeOption = 1,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Follow an accessible PR without starting an AI review."""
    row = run(follow_configured(config, forge, pr))
    if json_output:
        typer.echo(row.model_dump_json())
    else:
        print_row(row)


@app.command()
def review(
    pr: str,
    config: ConfigOption = None,
    login: str | None = None,
    reviewer: str | None = None,
) -> None:
    """Explicitly run a source-only native review of a synced/followed PR."""
    result = run(review_configured(config, pr, login, reviewer))
    typer.echo(result.model_dump_json())
    if result.status == "failed":
        raise typer.Exit(1)


@app.command()
def findings(pr: str, config: ConfigOption = None, login: str | None = None) -> None:
    """Read retained orientation/findings from local storage, including after cleanup."""
    result = run(findings_configured(config, pr, login))
    typer.echo(
        "No saved review result."
        if result is None
        else result.model_dump_json(exclude={"stdout", "stderr"})
    )


@app.command()
def approved(pr: str, config: ConfigOption = None, login: str | None = None) -> None:
    """Record your completed approval locally and clean owned resources; never post it."""
    status = run(approved_configured(config, pr, login))
    typer.echo("Completion recorded locally.")
    print_cleanup(status)


def print_cleanup(status: CleanupStatus) -> None:
    typer.echo(
        f"Cleanup jobs (all history): {status.succeeded} succeeded, {status.pending} pending, "
        f"{status.running} running, {status.failed} failed."
    )
    if status.failed:
        typer.echo("Cleanup failed; resources retained for retry after backoff.", err=True)
        raise typer.Exit(1)


@app.command()
def cleanup(config: ConfigOption = None) -> None:
    """Retry due completion cleanup jobs without starting reviews."""
    print_cleanup(run(cleanup_configured(config)))


@app.command()
def session(pr: str, config: ConfigOption = None, login: str | None = None) -> None:
    """Explicitly resume the exact saved native conversation in this terminal."""
    from diffbutler.ui.tui.foreground import ForegroundExitUncertain, run_foreground

    async def attach() -> int:
        try:
            async with handoff_configured(config, pr, login) as (launch, started):
                return await run_foreground(
                    launch.argv,
                    cwd=launch.cwd,
                    env=foreground_environment(launch),
                    started=started,
                )
        except ForegroundExitUncertain:
            raise NativeError("Foreground exit is uncertain; ownership retained") from None

    code = run(attach())
    if code:
        raise typer.Exit(code)


def main(argv: Sequence[str] | None = None) -> None:
    """Preserve callable success behavior while Typer handles usage errors."""
    arguments = list(argv) if argv is not None else sys.argv[1:]
    try:
        app(args=arguments, prog_name="diffbutler")
    except SystemExit as exc:
        if exc.code or any(arg in {"--help", "--version"} for arg in arguments):
            raise
