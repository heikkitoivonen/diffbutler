"""Typer interface to the shared core; terminal rendering stays here."""

import asyncio
import json
import logging
import sys
from collections.abc import Coroutine, Sequence
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import TypeAdapter
from sqlalchemy.exc import SQLAlchemyError

from diffbutler.config.diagnostics import configure_logging, stop_logging
from diffbutler.config.file import show_config_file
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
from diffbutler.core.reset import reset_directories, reset_paths
from diffbutler.core.sync import PullRequestRow
from diffbutler.domain.forge import PRPage, PRQuery, PullRequest
from diffbutler.domain.lifecycle import CleanupStatus
from diffbutler.domain.review import ReviewConflict
from diffbutler.forges.base import ForgeError
from diffbutler.reviewers.native import NativeError
from diffbutler.sources.git import SourceError
from diffbutler.store.database import DatabaseError
from diffbutler.ui.cli.editor import edit_file

app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="A terminal dashboard for human-directed AI code reviews.",
    epilog="Run diffbutler dashboard for the interactive queue, or history for completed cycles.",
)
ConfigOption = Annotated[Path | None, typer.Option(help="Explicit TOML configuration file")]
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
        logging.getLogger(__name__).exception("CLI operation failed")
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    except TimeoutError:
        logging.getLogger(__name__).exception("CLI operation timed out")
        typer.echo("Error: operation timed out; inspect the saved run status", err=True)
        raise typer.Exit(1) from None
    except OSError, SQLAlchemyError:
        logging.getLogger(__name__).exception("CLI storage operation failed")
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
        bool, typer.Option("--version", "-v", callback=show_version, is_eager=True)
    ] = False,
) -> None:
    if context.invoked_subcommand != "reset":
        try:
            configure_logging()
        except OSError:
            typer.echo(
                "Warning: diagnostic log unavailable; check state directory permissions.", err=True
            )
        logging.getLogger(__name__).info(
            "Application startup command=%s", context.invoked_subcommand or "default"
        )
    if context.invoked_subcommand is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            dashboard()
        else:
            typer.echo(context.get_help())


@app.command()
def config(
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML file to show or create with defaults")
    ] = None,
    edit: Annotated[bool, typer.Option("--edit", help="Open the config file in $EDITOR")] = False,
) -> None:
    """Show or edit the config file, creating defaults if missing, then print its path."""
    path, content = run(asyncio.to_thread(show_config_file, config))
    if edit:
        run(edit_file(path))
    else:
        typer.echo(content, nl=False)
        if content and not content.endswith("\n"):
            typer.echo()
    typer.echo(f"\nConfig file: {path}")


@app.command()
def reset(
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Remove configuration, data, and diagnostic logs without confirmation"
        ),
    ] = False,
) -> None:
    """Remove standard DiffButler configuration, data, and state directories."""
    paths = reset_paths()
    typer.echo("This will permanently remove these DiffButler directories and their contents:")
    for path in paths:
        typer.echo(f"  {safe(str(path))}")
    if not force:
        typer.confirm("Are you sure?", default=False, abort=True)
    stop_logging()
    run(reset_directories(paths))
    typer.echo("DiffButler configuration, data, and diagnostic logs removed.")


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
    """Read saved review drafts or legacy findings, including after cleanup."""
    result = run(findings_configured(config, pr, login))
    typer.echo(
        "No saved review result."
        if result is None
        else "\n".join(safe(line) for line in result.markdown.split("\n"))
        if result.markdown is not None
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
    from diffbutler.core.application import local_store
    from diffbutler.core.managed import resolve_local

    async def pending() -> bool:
        async with local_store(config) as (queue, _):
            return (await resolve_local(queue, pr, login)).tracked.refresh_pending

    if run(pending()):
        typer.echo("Refresh pending; run sync to reconcile forge activity.", err=True)
    if code:
        raise typer.Exit(code)


def main(argv: Sequence[str] | None = None) -> None:
    """Preserve callable success behavior while Typer handles usage errors."""
    arguments = list(argv) if argv is not None else sys.argv[1:]
    try:
        app(args=arguments, prog_name="diffbutler")
    except Exception:
        logging.getLogger(__name__).exception("Unhandled application failure")
        raise
    except SystemExit as exc:
        if exc.code or any(arg in {"-h", "--help", "-v", "--version"} for arg in arguments):
            raise


@app.command()
def dashboard(config: ConfigOption = None, offline: bool = False) -> None:
    """Open the dashboard; --offline reads local state without polling the forge."""
    from diffbutler.config.settings import load_settings
    from diffbutler.ui.tui.dashboard import Dashboard

    async def launch() -> None:
        from diffbutler.core.setup import startup_settings

        async def ask(label: str, default: str) -> str:
            return typer.prompt(label, default=default or None)

        settings = (
            await asyncio.to_thread(load_settings, config)
            if offline
            else await startup_settings(
                config,
                directory=Path.cwd(),
                ask=ask if sys.stdin.isatty() and sys.stdout.isatty() else None,
                progress=lambda message: typer.echo(message, err=True),
            )
        )
        await Dashboard(config, settings, offline=offline).run_async()

    run(launch())


@app.command()
def waiting(pr: str, config: ConfigOption = None, login: str | None = None) -> None:
    """Explicitly wait for author changes without posting feedback."""
    from diffbutler.core.dashboard import state_configured

    run(state_configured(config, pr, login, waiting=True))
    typer.echo("Waiting for author changes.")


@app.command(name="open")
def open_pr(pr: str, config: ConfigOption = None, login: str | None = None) -> None:
    """Choose manual review and open the browser; never submit approval."""
    from diffbutler.config.settings import load_settings
    from diffbutler.core.dashboard import state_configured
    from diffbutler.ui.tui.browser import open_browser

    async def open_selected() -> None:
        url = await state_configured(config, pr, login, waiting=False)
        settings = await asyncio.to_thread(load_settings, config)
        try:
            opened = await open_browser(url, settings.ui.browser_command)
        except OSError, TimeoutError:
            opened = False
        typer.echo(
            "Manual review chosen. " + ("Browser opened." if opened else "Open: " + safe(url))
        )

    run(open_selected())


@app.command()
def history(
    config: ConfigOption = None,
    repo: str | None = None,
    number: int | None = None,
    reason: str | None = None,
    login: str | None = None,
    since: str | None = None,
    until: str | None = None,
    task: str | None = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read completed cycles offline, or --task UUID for retained analysis and receipts."""
    from datetime import UTC, datetime
    from uuid import UUID

    from diffbutler.core.dashboard import HistoryFilter, detail_configured, history_configured
    from diffbutler.domain.dashboard import CycleRecord

    def date(value: str | None) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except ValueError:
            raise typer.BadParameter("Dates must use ISO format") from None

    if task:
        try:
            task_id = UUID(task)
        except ValueError:
            raise typer.BadParameter("--task must be a cycle UUID") from None
        detail = run(detail_configured(config, task_id))
        typer.echo(detail.model_dump_json())
        return
    rows = run(
        history_configured(
            config,
            HistoryFilter(
                repository=repo,
                number=number,
                reason=reason,
                login=login,
                since=date(since),
                until=date(until),
            ),
        )
    )
    if json_output:
        typer.echo(TypeAdapter(tuple[CycleRecord, ...]).dump_json(rows).decode())
    else:
        for row in rows:
            print_pr(
                row.pull_request, f"cycle {row.cycle} · {row.completion_reason}", row.account.login
            )
            typer.echo(safe(f"  {row.task_id} · {row.completed_at} · cleanup {row.cleanup_status}"))
