"""Private, bounded application diagnostics without agent or forge content."""

import copy
import logging
import os
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

from diffbutler.config.settings import ConfigError
from diffbutler.sources.git import SourceError


def state_path() -> Path:
    return (
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state").expanduser()
        / "diffbutler"
    )


def log_path() -> Path:
    return state_path() / "logs" / "diffbutler.log"


type ExceptionInfo = (
    tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]
)


class DiagnosticFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        record = copy.copy(record)
        record.exc_text = None
        return super().format(record)

    def formatException(
        self,
        ei: ExceptionInfo,
    ) -> str:
        # Exception messages, source lines, and locals can contain remote content or secrets.
        # These exceptions guarantee safe diagnostics without raw command output.
        lines = ["Traceback (most recent call last):"]
        for frame in traceback.extract_tb(ei[2]):
            lines.append(f"  {Path(frame.filename).name}:{frame.lineno} in {frame.name}")
        lines.append(type(ei[1]).__name__)
        if isinstance(ei[1], (ConfigError, SourceError)):
            lines.append(str(ei[1]))
        return "\n".join(lines)


class DiagnosticHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # A full disk must not print logging failures into an interactive agent's terminal.
        pass


def stop_logging() -> None:
    logger = logging.getLogger("diffbutler")
    if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        logger.addHandler(logging.NullHandler())
    for handler in tuple(logger.handlers):
        if isinstance(handler, DiagnosticHandler):
            logger.removeHandler(handler)
            handler.close()


def configure_logging() -> Path:
    path = log_path()
    stop_logging()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.close(fd)
    path.chmod(0o600)
    handler = DiagnosticHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(
        DiagnosticFormatter("%(asctime)sZ %(levelname)s pid=%(process)d %(name)s %(message)s")
    )
    logger = logging.getLogger("diffbutler")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return path
