"""Shared application defaults for DiffButler's terminal interfaces."""

from collections.abc import Iterable
from typing import Any, ClassVar

from textual.app import App, SystemCommand
from textual.binding import Binding, BindingType
from textual.driver import Driver
from textual.screen import Screen
from textual.widgets import HelpPanel

from diffbutler.ui.tui.terminal_theme import (
    TerminalBackground,
    ThemeDetectingDriver,
    environment_theme,
)


class TerminalApp(App[None]):
    """Use shared theme and command defaults for the terminal interfaces."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "hide_help_panel", "Close keys", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.theme = environment_theme()
        self._initial_theme = self.theme
        self._theme_selected = False

    def get_driver_class(self) -> type[Driver]:
        driver = super().get_driver_class()
        # Preserve explicitly configured drivers and headless test behavior.
        from textual.drivers.linux_driver import LinuxDriver

        return ThemeDetectingDriver if driver is LinuxDriver else driver

    def action_change_theme(self) -> None:
        self._theme_selected = True
        super().action_change_theme()

    def on_terminal_background(self, message: TerminalBackground) -> None:
        if not self._theme_selected and self.theme == self._initial_theme:
            self.theme = message.theme

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "hide_help_panel":
            return bool(self.screen.query(HelpPanel))
        return super().check_action(action, parameters)

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        # Textual leaves the Screen type parameter unspecified in this method.
        commands = super().get_system_commands(screen)  # pyright: ignore[reportUnknownMemberType]
        return (command for command in commands if command.title != "Screenshot")

    def action_screenshot(self, filename: str | None = None, path: str | None = None) -> None:
        """Disable Textual's inherited screenshot action."""
