"""Shared application defaults for DiffButler's terminal interfaces."""

from collections.abc import Iterable
from typing import Any

from textual.app import App, SystemCommand
from textual.screen import Screen


class TerminalApp(App[None]):
    """Use shared theme and command defaults for the terminal interfaces."""

    def __init__(self) -> None:
        super().__init__()
        self.theme = "ansi-dark"

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        # Textual leaves the Screen type parameter unspecified in this method.
        commands = super().get_system_commands(screen)  # pyright: ignore[reportUnknownMemberType]
        return (command for command in commands if command.title != "Screenshot")

    def action_screenshot(self, filename: str | None = None, path: str | None = None) -> None:
        """Disable Textual's inherited screenshot action."""
