"""Command-line entry point for the project scaffold."""

import argparse
import asyncio
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path

from diffbutler.core.doctor import run_doctor


def main(argv: Sequence[str] | None = None) -> None:
    """Show package information or run local and configured-forge diagnostics."""
    parser = argparse.ArgumentParser(
        prog="diffbutler",
        description="A terminal dashboard for human-directed AI code reviews.",
        epilog="Early development: the dashboard and review commands are not implemented yet.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('diffbutler')}")
    commands = parser.add_subparsers(dest="command")
    doctor = commands.add_parser(
        "doctor", help="Validate configuration and initialize/check SQLite"
    )
    doctor.add_argument("--config", type=Path, help="Explicit TOML file (must exist)")
    doctor.add_argument("--repo", help="Select per-repository review overrides")
    doctor.add_argument(
        "--offline", action="store_true", help="Skip forge authentication/network checks"
    )
    doctor.add_argument("--db-url", help="Override db.url")
    doctor.add_argument(
        "--no-migrate", action="store_true", help="Check schema without applying migrations"
    )
    args = parser.parse_args(argv)
    if args.command != "doctor":
        parser.print_help()
        return
    db: dict[str, object] = {}
    if args.db_url is not None:
        db["url"] = args.db_url
    if args.no_migrate:
        db["auto_migrate"] = False
    report = asyncio.run(
        run_doctor(
            args.config, repo=args.repo, overrides={"db": db} if db else None, offline=args.offline
        )
    )
    for check in report.checks:
        print(f"{check.status:4}  {check.name:20}  {check.detail}")
    if not report.passed:
        raise SystemExit(1)
