"""Shared terminal command and keyboard behavior."""

import asyncio
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, call

import pytest
from textual.command import CommandPalette
from textual.widgets import HelpPanel, Static

from diffbutler.config.settings import load_settings
from diffbutler.core.dashboard import DashboardRow
from diffbutler.ui.tui.dashboard import Dashboard
from diffbutler.ui.tui.handoff_probe import HandoffProbe


@pytest.mark.asyncio
async def test_failed_review_status_includes_reason_and_output_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("")
    app = Dashboard(config, load_settings(config), offline=True)
    row = Mock(spec=DashboardRow, row=Mock(tracked=Mock(task_id="task")))
    monkeypatch.setattr(app, "selected", Mock(return_value=row))
    monkeypatch.setattr(app, "selected_key", Mock(return_value="task"))
    monkeypatch.setattr(app, "reload", AsyncMock())
    reason = "Codex conversation is busy (thread already has an active writer)."
    review = AsyncMock(return_value=Mock(status="failed", status_reason=reason))
    monkeypatch.setattr("diffbutler.ui.tui.dashboard.review_configured", review)
    status = Mock()
    monkeypatch.setattr(app, "status", status)
    app.action_run_review()
    await app.review_tasks["task"]
    status.assert_any_call(f"AI review failed: {reason} Press L for output.")
    review.assert_awaited_once()
    assert not app.review_tasks


@pytest.mark.parametrize("code", [0, 1, 127, -15])
@pytest.mark.asyncio
async def test_agent_exit_status_only_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("")
    app = Dashboard(config, load_settings(config), offline=True)
    handoff = MagicMock()
    handoff.__aenter__.return_value = (Mock(), Mock())
    monkeypatch.setattr(
        "diffbutler.ui.tui.dashboard.handoff_configured", Mock(return_value=handoff)
    )
    monkeypatch.setattr("diffbutler.ui.tui.dashboard.foreground_environment", Mock(return_value={}))
    monkeypatch.setattr("diffbutler.ui.tui.dashboard.run_foreground", AsyncMock(return_value=code))
    monkeypatch.setattr(app, "suspend", nullcontext)
    status = Mock()
    monkeypatch.setattr(app, "status", status)

    await app.attach(Mock(spec=DashboardRow, row=Mock()))

    assert not app.attached
    handoff.__aexit__.assert_awaited_once()
    expected = [call("Preparing agent session…"), call("")]
    if code != 0:
        expected.append(
            call(f"Agent exited {code}; forge refresh attempted. Check refresh-pending badges.")
        )
    assert status.call_args_list == expected


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


@pytest.mark.serial
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


@pytest.mark.serial
@pytest.mark.asyncio
async def test_handoff_heartbeat_stops_before_shutdown_removes_widgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ShutdownProbe(HandoffProbe):
        outcomes: list[object]
        ticks_at_shutdown: int

        async def _close_all(self) -> None:
            # Textual marks the app stopped before pruning screens, but cancels
            # app workers later. Force a heartbeat to wake in that interval.
            assert not self.is_running
            self.ticks_at_shutdown = self.ticks
            heartbeat = next(worker for worker in self.workers if worker.name == "heartbeat")
            await self.query_one("#heartbeat", Static).remove()
            self.outcomes = list(
                await asyncio.wait_for(
                    asyncio.gather(heartbeat.wait(), return_exceptions=True), timeout=2
                )
            )
            await super()._close_all()

    app = ShutdownProbe(tmp_path, ["unused"], tmp_path / "events.jsonl")
    monkeypatch.setattr(app, "background_output", AsyncMock())
    async with app.run_test() as pilot:
        async with asyncio.timeout(2):
            while not app.ticks:
                await pilot.pause()
        assert "Background ticks:" in str(app.query_one("#heartbeat", Static).content)
    assert app.outcomes == [None]
    assert app.ticks == app.ticks_at_shutdown


@pytest.mark.serial
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
