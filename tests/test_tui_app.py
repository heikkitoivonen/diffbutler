"""The terminal interfaces do not expose Textual's screenshot command."""

from pathlib import Path

import pytest

from diffbutler.config.settings import load_settings
from diffbutler.ui.tui.dashboard import Dashboard
from diffbutler.ui.tui.handoff_probe import HandoffProbe


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
