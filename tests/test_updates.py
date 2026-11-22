"""Checking PyPI for a newer DiffButler release."""

import asyncio
import re
from importlib.metadata import version
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from diffbutler.config.settings import load_settings
from diffbutler.core.updates import (
    CHECK_INTERVAL_S,
    PYPI_URL,
    UpdateCheck,
    UpdateCheckError,
    check_for_update,
    latest_release,
)
from diffbutler.ui.tui.dashboard import Dashboard, UpdateBadge


@pytest.mark.parametrize(
    "installed,latest,available",
    [
        ("0.8.0", "0.9.0", True),
        ("0.8.0", "0.10.0", True),
        ("0.8.0", "1.0", True),
        ("0.8.0+local", "0.8.1", True),
        ("0.8.0", "0.8.0", False),
        ("0.9.0", "0.8.0", False),
        ("0.9", "0.9.0", False),
        ("0.9.0rc1", "0.9.0", False),
        ("0.9.0.dev3", "0.9.1", True),
        ("0.8.0", "0.9.0.post1", True),
        ("0.9.0", "0.9.0.post1", False),
    ],
)
def test_only_newer_release_numbers_are_available(
    installed: str, latest: str, available: bool
) -> None:
    assert UpdateCheck(installed, latest).available is available


def pypi(response: httpx.Response | Exception) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    return httpx.MockTransport(handle), requests


@pytest.mark.parametrize("latest", ["0.9.0", "0.9.0.post1"])
@pytest.mark.asyncio
async def test_latest_release_reads_pypi_project_version(latest: str) -> None:
    transport, requests = pypi(httpx.Response(200, json={"info": {"version": latest}}))
    assert await latest_release(transport) == latest
    assert [str(request.url) for request in requests] == [PYPI_URL]
    assert requests[0].headers["User-Agent"] == "DiffButler"


@pytest.mark.asyncio
async def test_check_compares_with_installed_distribution() -> None:
    transport, _ = pypi(httpx.Response(200, json={"info": {"version": "0.9.0"}}))
    assert await check_for_update(transport) == UpdateCheck(version("diffbutler"), "0.9.0")


@pytest.mark.asyncio
async def test_unrecognized_installed_version_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("diffbutler.core.updates.version", Mock(return_value="unknown"))
    transport, requests = pypi(httpx.Response(200, json={"info": {"version": "0.9.0"}}))
    with pytest.raises(UpdateCheckError, match=r"^Installed version is unrecognized$"):
        await check_for_update(transport)
    assert not requests


@pytest.mark.parametrize(
    "response,message",
    [
        (httpx.Response(404), "PyPI returned HTTP 404"),
        (httpx.Response(200, content=b"<html>"), "PyPI returned invalid JSON"),
        (httpx.Response(200, json=["0.9.0"]), "PyPI response has no version"),
        (httpx.Response(200, json={"info": {"version": 9}}), "PyPI response has no version"),
        (httpx.Response(200, json={"info": {"version": ""}}), "PyPI response has no version"),
        (
            httpx.Response(200, json={"info": {"version": "0.9.0rc1"}}),
            "PyPI reported an unrecognized version",
        ),
        (
            httpx.Response(200, json={"info": {"version": "not a version"}}),
            "PyPI reported an unrecognized version",
        ),
        (httpx.ConnectError("refused"), "PyPI unreachable (ConnectError)"),
    ],
)
@pytest.mark.asyncio
async def test_unusable_pypi_responses_raise_update_check_error(
    response: httpx.Response | Exception, message: str
) -> None:
    transport, _ = pypi(response)
    with pytest.raises(UpdateCheckError, match=f"^{re.escape(message)}$"):
        await latest_release(transport)


def dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, offline: bool, enabled: bool = True
) -> Dashboard:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[db]\nurl = "sqlite+aiosqlite:///{tmp_path}/dashboard.db"\n'
        f"[general]\ncheck_for_updates = {str(enabled).lower()}\n"
    )
    app = Dashboard(config, load_settings(config), offline=offline)
    # Update checks are under test, not forge polling.
    monkeypatch.setattr(app.poller, "run", AsyncMock())
    return app


async def settle(app: Dashboard) -> None:
    async with asyncio.timeout(10):
        while any(worker.group == "update-check" for worker in app.workers):
            await asyncio.sleep(0.01)


def badge(app: Dashboard) -> str:
    return str(app.query_one(UpdateBadge).render())


@pytest.mark.serial
@pytest.mark.asyncio
async def test_startup_check_shows_badge_and_announces_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = dashboard(tmp_path, monkeypatch, offline=False)
    check = AsyncMock(return_value=UpdateCheck("0.8.0", "0.9.0"))
    monkeypatch.setattr("diffbutler.ui.tui.dashboard.check_for_update", check)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        check.assert_awaited_once_with()
        assert badge(app) == "↑ 0.9.0"
        region = app.query_one(UpdateBadge).region
        assert (region.y, region.right) == (0, 80)
        assert str(app.query_one("#status").render()) == (
            "DiffButler 0.9.0 is available (installed 0.8.0)."
        )


@pytest.mark.serial
@pytest.mark.asyncio
async def test_scheduled_checks_of_the_same_release_stay_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = dashboard(tmp_path, monkeypatch, offline=False)
    check = AsyncMock(return_value=UpdateCheck("0.8.0", "0.9.0"))
    monkeypatch.setattr("diffbutler.ui.tui.dashboard.check_for_update", check)
    # The daily timer fires quickly here; only its repeated checks are under test.
    monkeypatch.setattr("diffbutler.ui.tui.dashboard.CHECK_INTERVAL_S", 0.2)
    async with app.run_test(size=(80, 24)) as pilot:
        async with asyncio.timeout(10):
            while app.announced_update is None:
                await pilot.pause(0.01)
        app.status("Forge refreshed")
        seen = check.await_count
        async with asyncio.timeout(10):
            while check.await_count <= seen:
                await pilot.pause(0.05)
        await settle(app)
        assert badge(app) == "↑ 0.9.0"
        assert str(app.query_one("#status").render()) == "Forge refreshed"


@pytest.mark.serial
@pytest.mark.asyncio
async def test_scheduled_check_leaves_a_running_manual_check_to_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = dashboard(tmp_path, monkeypatch, offline=True)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> UpdateCheck:
        started.set()
        await release.wait()
        return UpdateCheck("0.8.0", "0.8.0")

    check = AsyncMock(side_effect=slow)
    monkeypatch.setattr("diffbutler.ui.tui.dashboard.check_for_update", check)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+u")
        await asyncio.wait_for(started.wait(), timeout=10)
        app.start_update_check(manual=False)
        release.set()
        await settle(app)
        check.assert_awaited_once_with()
        assert str(app.query_one("#status").render()) == "DiffButler 0.8.0 is up to date."


def test_checks_are_daily() -> None:
    assert CHECK_INTERVAL_S == 24 * 60 * 60


@pytest.mark.serial
@pytest.mark.asyncio
async def test_notice_hidden_by_a_session_is_announced_by_the_next_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = dashboard(tmp_path, monkeypatch, offline=True)
    monkeypatch.setattr(
        "diffbutler.ui.tui.dashboard.check_for_update",
        AsyncMock(return_value=UpdateCheck("0.8.0", "0.9.0")),
    )
    async with app.run_test(size=(80, 24)):
        app.update_timer = app.set_interval(3600, lambda: None)  # Automatic checks enabled.
        app.attached = True
        app.start_update_check(manual=False)
        await settle(app)
        assert app.announced_update is None
        app.attached = False
        app.start_update_check(manual=False)
        await settle(app)
        assert app.announced_update == "0.9.0"
        assert str(app.query_one("#status").render()) == (
            "DiffButler 0.9.0 is available (installed 0.8.0)."
        )


@pytest.mark.serial
@pytest.mark.parametrize("offline,enabled", [(True, True), (False, False)])
@pytest.mark.asyncio
async def test_no_automatic_check_offline_or_disabled_but_ctrl_u_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: bool, enabled: bool
) -> None:
    app = dashboard(tmp_path, monkeypatch, offline=offline, enabled=enabled)
    check = AsyncMock(return_value=UpdateCheck("0.8.0", "0.8.0"))
    monkeypatch.setattr("diffbutler.ui.tui.dashboard.check_for_update", check)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await settle(app)
        check.assert_not_awaited()
        assert app.update_timer is None
        await pilot.press("ctrl+u")
        await settle(app)
        check.assert_awaited_once_with()
        assert badge(app) == ""
        assert str(app.query_one("#status").render()) == "DiffButler 0.8.0 is up to date."


@pytest.mark.serial
@pytest.mark.asyncio
async def test_failed_check_is_reported_only_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = dashboard(tmp_path, monkeypatch, offline=False)
    check = AsyncMock(side_effect=UpdateCheckError("PyPI unreachable (ConnectError)"))
    monkeypatch.setattr("diffbutler.ui.tui.dashboard.check_for_update", check)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app)
        check.assert_awaited_once_with()
        assert "PyPI" not in str(app.query_one("#status").render())
        await pilot.press("ctrl+u")
        await settle(app)
        assert str(app.query_one("#status").render()) == (
            "Update check failed: PyPI unreachable (ConnectError)"
        )


@pytest.mark.serial
@pytest.mark.asyncio
async def test_disabling_in_settings_stops_checks_and_clears_badge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = dashboard(tmp_path, monkeypatch, offline=False)
    check = AsyncMock(return_value=UpdateCheck("0.8.0", "0.9.0"))
    monkeypatch.setattr("diffbutler.ui.tui.dashboard.check_for_update", check)
    async with app.run_test(size=(80, 24)):
        await settle(app)
        assert badge(app) == "↑ 0.9.0"
        assert app.update_timer is not None and app.path is not None
        app.path.write_text(app.path.read_text().replace("= true", "= false"))
        await app.apply_settings()
        assert app.update_timer is None
        assert badge(app) == ""
        app.path.write_text(app.path.read_text().replace("= false", "= true"))
        await app.apply_settings()
        await settle(app)
        # Re-enabling checks immediately, then daily.
        assert check.await_count == 2
        assert app.update_timer is not None
        assert badge(app) == "↑ 0.9.0"


@pytest.mark.serial
@pytest.mark.asyncio
async def test_check_running_when_disabled_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = dashboard(tmp_path, monkeypatch, offline=False)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> UpdateCheck:
        started.set()
        await release.wait()
        return UpdateCheck("0.8.0", "0.9.0")

    monkeypatch.setattr("diffbutler.ui.tui.dashboard.check_for_update", slow)
    async with app.run_test(size=(80, 24)):
        await asyncio.wait_for(started.wait(), timeout=10)
        assert app.path is not None
        app.path.write_text(app.path.read_text().replace("= true", "= false"))
        await app.apply_settings()
        release.set()
        await settle(app)
        assert app.update_timer is None
        assert badge(app) == ""
        assert app.announced_update is None


def test_settings_toggle_follows_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("")
    assert load_settings(config).general.check_for_updates is True
    monkeypatch.setenv("DIFFBUTLER_GENERAL__CHECK_FOR_UPDATES", "false")
    assert load_settings(config).general.check_for_updates is False
