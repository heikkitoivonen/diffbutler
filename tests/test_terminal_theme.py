"""Offline terminal background detection and input isolation."""

import pytest

from diffbutler.ui.tui.app import TerminalApp
from diffbutler.ui.tui.terminal_theme import (
    BackgroundInput,
    TerminalBackground,
    background_theme,
    environment_theme,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("rgb:ffff/ffff/ffff", "ansi-light"),
        ("rgb:fafa/fafa/fafa", "ansi-light"),
        ("rgb:ff/ff/ff", "ansi-light"),
        ("rgb:f/f/f", "ansi-light"),
        ("rgb:0000/0000/0000", "ansi-dark"),
        ("rgb:28/28/28", "ansi-dark"),
        ("rgb:002b/3636/0000", "ansi-dark"),
        ("rgb:ffff/ffff", None),
        ("rgb:zz/zz/zz", None),
        ("rgb:fffff/fffff/fffff", None),
        ("", None),
    ],
)
def test_background_theme(value: str, expected: str | None) -> None:
    assert background_theme(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "ansi-dark"),
        ("", "ansi-dark"),
        ("15;0", "ansi-dark"),
        ("0;7", "ansi-light"),
        ("0;default;15", "ansi-light"),
        ("0;231", "ansi-light"),
        ("0;232", "ansi-dark"),
        ("0;256", "ansi-dark"),
        ("0;-1", "ansi-dark"),
        ("invalid", "ansi-dark"),
        ("0;²", "ansi-dark"),
    ],
)
def test_environment_theme(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str
) -> None:
    monkeypatch.delenv("COLORFGBG", raising=False)
    if value is not None:
        monkeypatch.setenv("COLORFGBG", value)
    assert environment_theme() == expected


@pytest.mark.parametrize("ending", ["\x07", "\x1b\\"])
def test_background_reply_split_at_every_boundary_preserves_keys(ending: str) -> None:
    response = f"\x1b]11;rgb:ffff/ffff/ffff{ending}"
    data = f"x{response}\x1b[Aq"
    for boundary in range(len(data) + 1):
        colors = BackgroundInput()
        first, first_theme = colors.feed(data[:boundary])
        second, second_theme = colors.feed(data[boundary:])
        assert first + second == "x\x1b[Aq"
        assert (first_theme or second_theme) == "ansi-light"


def test_pasted_color_report_does_not_change_theme() -> None:
    colors = BackgroundInput()
    data = "\x1b[200~\x1b]11;rgb:ffff/ffff/ffff\x07\x1b[201~"
    output = ""
    for character in data:
        keys, theme = colors.feed(character)
        output += keys
        assert theme is None
    assert output == data


def test_late_and_duplicate_replies_are_consumed_without_changing_theme() -> None:
    response = "\x1b]11;rgb:ffff/ffff/ffff\x07"
    colors = BackgroundInput()
    assert colors.feed(response) == ("", "ansi-light")
    assert colors.feed(response + "q") == ("q", None)
    colors = BackgroundInput()
    colors.deadline = 0
    assert colors.feed(response + "q") == ("q", None)


def test_escape_key_and_invalid_report() -> None:
    colors = BackgroundInput()
    assert colors.feed("\x1b") == ("", None)
    assert colors.flush() == "\x1b"
    assert colors.feed("\x1b]11;invalid\x07q") == ("q", None)


def test_slow_reply_is_not_flushed_as_keyboard_input() -> None:
    colors = BackgroundInput()
    assert colors.feed("\x1b]11;rgb:ffff/") == ("", None)
    assert colors.flush() == ""
    assert colors.feed("ffff/ffff\x07q") == ("q", "ansi-light")


@pytest.mark.serial
@pytest.mark.asyncio
async def test_detection_preserves_manual_theme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLORFGBG", raising=False)
    app = TerminalApp()
    assert app.theme == "ansi-dark"
    async with app.run_test() as pilot:
        app.post_message(TerminalBackground("ansi-light"))
        await pilot.pause()
        assert app.theme == "ansi-light"
        app.theme = "nord"
        app.post_message(TerminalBackground("ansi-dark"))
        await pilot.pause()
        assert app.theme == "nord"


@pytest.mark.serial
@pytest.mark.asyncio
async def test_opening_theme_picker_preserves_manual_dark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COLORFGBG", raising=False)
    app = TerminalApp()
    async with app.run_test() as pilot:
        app.action_change_theme()
        app.post_message(TerminalBackground("ansi-light"))
        await pilot.pause()
        assert app.theme == "ansi-dark"
