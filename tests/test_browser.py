"""Browser dispatch respects local desktops and remote/headless sessions."""

from unittest.mock import Mock

import pytest

from diffbutler.ui.tui.browser import open_browser


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "environment", "expected"),
    [
        ("darwin", {}, True),
        ("darwin", {"SSH_CONNECTION": "remote"}, False),
        ("linux", {}, False),
        ("linux", {"DISPLAY": ":0"}, True),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, True),
        ("linux", {"BROWSER": "firefox"}, True),
        ("linux", {"SSH_CONNECTION": "remote", "DISPLAY": ":0"}, False),
    ],
)
async def test_default_browser_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    environment: dict[str, str],
    expected: bool,
) -> None:
    for name in ("SSH_CONNECTION", "BROWSER", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("diffbutler.ui.tui.browser.sys.platform", platform)
    launch = Mock(return_value=True)
    monkeypatch.setattr("diffbutler.ui.tui.browser.webbrowser.open", launch)

    url = "https://github.com/acme/api/pull/1"
    assert await open_browser(url) is expected
    if expected:
        launch.assert_called_once_with(url)
    else:
        launch.assert_not_called()


@pytest.mark.asyncio
async def test_macos_browser_failure_returns_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setattr("diffbutler.ui.tui.browser.sys.platform", "darwin")
    launch = Mock(return_value=False)
    monkeypatch.setattr("diffbutler.ui.tui.browser.webbrowser.open", launch)

    url = "https://github.com/acme/api/pull/1"
    assert not await open_browser(url)
    launch.assert_called_once_with(url)
