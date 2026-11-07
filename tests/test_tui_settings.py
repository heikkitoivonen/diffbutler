"""The dashboard edits settings in place and applies saved values without restarting."""

from pathlib import Path

import pytest
from rich.text import Text
from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, DataTable, Input, Static, TabbedContent

from diffbutler.config.settings import load_settings
from diffbutler.ui.tui.dashboard import Dashboard
from diffbutler.ui.tui.settings import EntryForm, FieldRow, SettingsScreen

pytestmark = [pytest.mark.serial, pytest.mark.asyncio]


def dashboard(tmp_path: Path, extra: str = "") -> tuple[Dashboard, Path]:
    config = tmp_path / "config.toml"
    config.write_text(
        f'# keep me\n[db]\nurl = "sqlite+aiosqlite:///{tmp_path}/dashboard.db"\n{extra}'
    )
    return Dashboard(config, load_settings(config), offline=True), config


async def open_settings(app: Dashboard, pilot: Pilot[None]) -> SettingsScreen:
    await pilot.press("comma")
    await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
    await pilot.pause()
    assert isinstance(app.screen, SettingsScreen)
    return app.screen


def text(row: FieldRow) -> Input:
    assert isinstance(row.control, Input)
    return row.control


def field(screen: SettingsScreen, path: str) -> FieldRow:
    return next(row for row in screen.query(FieldRow) if row.path == path)


async def save(app: Dashboard, pilot: Pilot[None]) -> None:
    await pilot.press("ctrl+s")
    for _ in range(3):
        await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
        await pilot.pause()


async def test_saved_settings_apply_to_the_running_dashboard(tmp_path: Path) -> None:
    app, config = dashboard(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_settings(app, pilot)
        text(field(screen, "general.poll_interval_s")).value = "900"
        text(field(screen, "ui.columns")).value = "number title"
        preview = field(screen, "ui.preview_pane").control
        assert isinstance(preview, Checkbox)
        preview.value = False
        await save(app, pilot)
        assert app.screen is not screen
        assert app.settings.general.poll_interval_s == 900
        assert app.poller.minimum == 900
        assert app.settings.ui.columns == ["number", "title"]
        assert app.settings.ui.preview_pane is False
        table = app.query_one(DataTable[Text])
        assert [str(column.label) for column in table.columns.values()] == ["PR", "Title"]
        assert app.sort.column == "state"  # Default sort column falls back from removed column.
        assert "Settings saved and applied" in str(app.query_one("#status", Static).content)
    content = config.read_text()
    assert content.startswith("# keep me\n")
    assert "poll_interval_s = 900" in content and "preview_pane = false" in content


async def test_saved_key_bindings_replace_earlier_ones(tmp_path: Path) -> None:
    app, config = dashboard(tmp_path, '[keys]\nopen_in_browser = ["o"]\n')
    async with app.run_test(size=(120, 40)) as pilot:
        assert "o" in app._bindings.key_to_bindings  # pyright: ignore[reportPrivateUsage]
        screen = await open_settings(app, pilot)
        form = next(form for form in screen.query(EntryForm) if form.section.name == "keys")
        text(form.rows[0]).value = "x"
        await save(app, pilot)
        bindings = app._bindings.key_to_bindings  # pyright: ignore[reportPrivateUsage]
        assert [binding.action for binding in bindings["x"]] == ["open_in_browser"]
        assert not bindings.get("o")
        assert any(binding.action == "command_palette" for binding in bindings["ctrl+p"])
    assert load_settings(config).keys == {"open_in_browser": ["x"]}


async def test_escape_confirms_before_discarding_changes(tmp_path: Path) -> None:
    app, config = dashboard(tmp_path)
    original = config.read_text()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_settings(app, pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is not screen  # Nothing changed, so it closes at once.
        screen = await open_settings(app, pilot)
        text(field(screen, "general.max_concurrent_reviews")).value = "5"
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is screen
        assert "Unsaved changes" in str(screen.query_one("#settings-status", Static).content)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is not screen
    assert config.read_text() == original


async def test_invalid_values_are_reported_and_can_be_corrected(
    tmp_path: Path,
) -> None:
    app, config = dashboard(tmp_path)
    original = config.read_text()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_settings(app, pilot)
        control = text(field(screen, "general.max_concurrent_reviews"))
        control.value = "many"
        await save(app, pilot)
        status = str(screen.query_one("#settings-status", Static).content)
        assert status == "Not saved: general.max_concurrent_reviews: enter a whole number"
        control.value = "0"
        await save(app, pilot)
        assert app.screen is screen
        status = str(screen.query_one("#settings-status", Static).content)
        assert "general.max_concurrent_reviews" in status
        assert config.read_text() == original
        # The failed save re-enables the form, so the value can be corrected.
        assert not screen.query_one(TabbedContent).disabled
        control.value = "3"
        await save(app, pilot)
        assert app.screen is not screen
    assert load_settings(config).general.max_concurrent_reviews == 3


async def press(screen: SettingsScreen, pilot: Pilot[None], pane: str, query: str) -> None:
    screen.query_one(TabbedContent).active = f"settings-{pane}"
    await pilot.pause()
    screen.query_one(f"#settings-{pane} {query}", Button).press()
    await pilot.pause()


async def test_entries_can_be_added_and_removed(tmp_path: Path) -> None:
    app, config = dashboard(tmp_path, '[repos."acme/old"]\nenabled = false\n')
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_settings(app, pilot)
        await press(screen, pilot, "forges", ".add")
        await press(screen, pilot, "forges", ".add")
        forges = [form for form in screen.query(EntryForm) if form.section.name == "forges"]
        assert [form.title for form in forges] == ["Forge 1 (forges.0)", "Forge 2 (forges.1)"]
        await press(screen, pilot, "forges", "EntryForm Button")
        forges = [form for form in screen.query(EntryForm) if form.section.name == "forges"]
        assert [form.title for form in forges] == ["Forge 1 (forges.0)"]
        text(next(row for row in forges[0].rows if row.spec.label == "login")).value = "me"
        await press(screen, pilot, "repos", ".add")
        await press(screen, pilot, "repos", "EntryForm Button")
        (repo,) = [form for form in screen.query(EntryForm) if form.section.name == "repos"]
        assert repo.origin is None and repo.name_input is not None
        repo.name_input.value = "acme/new"
        await save(app, pilot)
        assert app.screen is not screen
    settings = load_settings(config)
    assert [(forge.kind, forge.base_url, forge.login) for forge in settings.forges] == [
        ("github", "https://api.github.com", "me")
    ]
    assert set(settings.repos) == {"acme/new"}


async def test_enter_in_a_settings_field_stays_in_settings(tmp_path: Path) -> None:
    app, _ = dashboard(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_settings(app, pilot)
        control = text(field(screen, "general.poll_interval_s"))
        control.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is screen
        assert app.focused is not control


async def test_controls_are_disabled_while_saving(tmp_path: Path) -> None:
    app, config = dashboard(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_settings(app, pilot)
        text(field(screen, "general.max_concurrent_reviews")).value = "3"
        screen.action_save()
        assert screen.query_one(TabbedContent).disabled
        for _ in range(3):
            await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause()
        assert app.screen is not screen
    assert load_settings(config).general.max_concurrent_reviews == 3


async def test_environment_overrides_of_named_entries_are_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIFFBUTLER_KEYS__OPEN_IN_BROWSER", '["x"]')
    app, _ = dashboard(tmp_path, '[keys]\nopen_in_browser = ["o"]\nsettings = ["comma"]\n')
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_settings(app, pilot)
        notes = {
            form.origin: "environment variable"
            in str(form.rows[0].query_one(".help", Static).content)
            for form in screen.query(EntryForm)
            if form.section.name == "keys"
        }
        assert notes == {"open_in_browser": True, "settings": False}
