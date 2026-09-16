"""Command-line entry point for the project scaffold."""

import argparse
from collections.abc import Sequence
from importlib.metadata import version


def main(argv: Sequence[str] | None = None) -> None:
    """Show package information until the dashboard is implemented."""
    parser = argparse.ArgumentParser(
        prog="diffbutler",
        description="A terminal dashboard for human-directed AI code reviews.",
        epilog="Early development: the dashboard and review commands are not implemented yet.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('diffbutler')}")
    parser.parse_args(argv)
    parser.print_help()
