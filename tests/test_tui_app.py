"""Shared terminal command and keyboard behavior."""

from pathlib import Path

import pytest
from textual.command import CommandPalette
from textual.widgets import HelpPanel

from diffbutler.config.settings import load_settings
from diffbutler.ui.tui.dashboard import Dashboard
from diffbutler.ui.tui.handoff_probe import HandoffProbe


@pytest.mark.parametrize("authenticated,expected", [(True, 60), (False, 600)])
def test_dashboard_uses_account_polling_default(
    tmp_path: Path, authenticated: bool, expected: int
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[[forges]]\nkind="github"\nbase_url="https://api.github.com"\nlogin="reader"\n'
        + ('token_env="GITHUB_TOKEN"\n' if authenticated else "")
    )
    app = Dashboard(config, load_settings(config), offline=True)
    assert app.poller.interval == expected


@pytest.mark.parametrize("interface", ["dashboard", "handoff"])
@pytest.mark.asyncio
async def test_screenshots_are_disabled(
    interface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'[db]\nurl = "sqlite+aiosqlite:///{tmp_path}/dashboard.db"\n')
    app = (
        Dashboard(config, load_settings(config), offline=True)
        if interface == "dashboard"
        else HandoffProbe(tmp_path, ["unused"], tmp_path / "events.jsonl")
    )

    def unexpected_screenshot(*args: object, **kwargs: object) -> None:
        pytest.fail("Screenshot delivery must not be invoked")

    async def no_background_output() -> None:
        """Command tests do not need the handoff experiment's ticker subprocess."""

    if isinstance(app, HandoffProbe):
        monkeypatch.setattr(app, "background_output", no_background_output)
    monkeypatch.setattr(app, "deliver_screenshot", unexpected_screenshot)
    async with app.run_test() as pilot:
        commands = {command.title for command in app.get_system_commands(app.screen)}
        assert "Screenshot" not in commands
        assert {"Theme", "Quit", "Keys"} <= commands
        await app.run_action("screenshot")
        await pilot.pause()


@pytest.mark.asyncio
async def test_escape_closes_keys_panel_opened_from_palette(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'[db]\nurl = "sqlite+aiosqlite:///{tmp_path}/dashboard.db"\n')
    app = Dashboard(config, load_settings(config), offline=True)
    async with app.run_test() as pilot:
        main_screen = app.screen
        focused = app.focused
        await pilot.press("ctrl+p")
        await pilot.press("K", "e", "y", "s")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is main_screen
        assert app.screen.query(HelpPanel)
        await pilot.press("escape")
        await pilot.pause()
        assert not app.screen.query(HelpPanel)
        assert app.focused is focused

        # Escape still dismisses the palette when the Keys panel is absent.
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, CommandPalette)
        await pilot.press("escape")
        assert app.screen is main_screen
