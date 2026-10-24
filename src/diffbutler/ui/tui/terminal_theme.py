"""Detect the terminal background without competing with Textual for input."""

import os
import re
import selectors
import time
from codecs import getincrementaldecoder
from contextlib import suppress

from rich.color import Color as RichColor
from textual._parser import ParseError
from textual._xterm_parser import XTermParser
from textual.color import Color
from textual.drivers.linux_driver import LinuxDriver
from textual.message import Message

BACKGROUND_QUERY = "\x1b]11;?\x1b\\"
_BACKGROUND_PREFIX = "\x1b]11;"
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"
_RGB = re.compile(r"rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})")


def background_theme(value: str) -> str | None:
    """Interpret an OSC 11 RGB response, including 8- and 16-bit components."""
    match = _RGB.fullmatch(value)
    if match is None:
        return None
    red, green, blue = (
        round(int(component, 16) * 255 / (16 ** len(component) - 1)) for component in match.groups()
    )
    return "ansi-light" if Color(red, green, blue).brightness >= 0.5 else "ansi-dark"


def environment_theme() -> str:
    """Use the advertised ANSI background index when a terminal exports it."""
    background = os.environ.get("COLORFGBG", "").rsplit(";", 1)[-1]
    if (
        len(background) <= 3
        and background.isascii()
        and background.isdecimal()
        and 0 <= int(background) <= 255
    ):
        color = RichColor.from_ansi(int(background)).get_truecolor()
        if Color(*color).brightness >= 0.5:
            return "ansi-light"
    return "ansi-dark"


class TerminalBackground(Message):
    """The terminal's initial background theme, reported by the input thread."""

    def __init__(self, theme: str) -> None:
        super().__init__()
        self.theme = theme


class BackgroundInput:
    """Remove color replies from the key stream, preserving ordinary input and paste."""

    def __init__(self) -> None:
        self.pending = ""
        self.pasting = False
        self.reported = False
        self.deadline = time.monotonic() + 1

    def feed(self, data: str) -> tuple[str, str | None]:
        output: list[str] = []
        theme: str | None = None
        for character in data:
            self.pending += character
            while self.pending:
                if not self.pasting and self.pending.startswith(_BACKGROUND_PREFIX):
                    if self.pending.endswith(("\x07", "\x1b\\")):
                        value = self.pending[len(_BACKGROUND_PREFIX) :].rstrip("\x07\x1b\\")
                        if not self.reported and time.monotonic() <= self.deadline:
                            theme = background_theme(value)
                            self.reported = theme is not None
                        self.pending = ""
                    elif len(self.pending) > 128:
                        # A malformed report must not hold subsequent keyboard input.
                        self.pending = ""
                    break
                markers = (_PASTE_END,) if self.pasting else (_BACKGROUND_PREFIX, _PASTE_START)
                if self.pending in (_PASTE_START, _PASTE_END):
                    self.pasting = self.pending == _PASTE_START
                    output.append(self.pending)
                    self.pending = ""
                elif any(marker.startswith(self.pending) for marker in markers):
                    break
                else:
                    output.append(self.pending[0])
                    self.pending = self.pending[1:]
        return "".join(output), theme

    def flush(self) -> str:
        """Release an incomplete prefix on an idle tick (including the Escape key)."""
        # A recognized reply may arrive slowly over SSH. Keep it atomic so its
        # remaining RGB digits cannot turn into application shortcuts.
        if self.pending.startswith(_BACKGROUND_PREFIX):
            return ""
        pending, self.pending = self.pending, ""
        return pending


class ThemeDetectingDriver(LinuxDriver):
    """Use Textual's POSIX lifecycle with a color-aware input stream."""

    def run_input_thread(self) -> None:
        # Keep one input reader: terminal replies must never become key bindings.
        parser = XTermParser(self._debug)
        colors = BackgroundInput()
        decode = getincrementaldecoder("utf-8")().decode
        if not getattr(self, "_background_queried", False):
            self._background_queried = True
            if self.input_tty:
                self.write(BACKGROUND_QUERY)
                self.flush()
        else:
            # Returning from a foreground session must preserve the chosen theme.
            colors.reported = True

        def dispatch(data: str) -> None:
            if data:
                for event in parser.feed(data):
                    self.process_message(event)

        with selectors.SelectSelector() as selector:
            selector.register(self.fileno, selectors.EVENT_READ)
            try:
                while not self.exit_event.is_set():
                    if selector.select(0.1):
                        data = os.read(self.fileno, 4096)
                        if not data:
                            break
                        keys, theme = colors.feed(decode(data))
                        if theme is not None:
                            self.process_message(TerminalBackground(theme))
                        dispatch(keys)
                    else:
                        dispatch(colors.flush())
                    for event in parser.tick():
                        self.process_message(event)
            finally:
                with suppress(EOFError, ParseError):
                    list(parser.feed(""))
