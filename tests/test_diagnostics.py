"""Private diagnostic files, exception handling, and bounded retention."""

import logging
from pathlib import Path

import pytest

from diffbutler.config.diagnostics import (
    DiagnosticHandler,
    configure_logging,
    log_path,
    state_path,
    stop_logging,
)
from diffbutler.config.settings import ConfigError
from diffbutler.sources.git import SourceError
from diffbutler.ui.cli import main


def test_diagnostic_tracebacks_exclude_untrusted_messages(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = configure_logging()
    logger = logging.getLogger("diffbutler.test")
    try:
        try:
            raise ValueError("secret-token private PR title")
        except ValueError:
            logger.exception("Operation failed")
        try:
            raise ConfigError("Native review requires a configured local repository path")
        except ConfigError:
            logger.exception("Review failed")
        try:
            raise SourceError("Git fetch failed (exit 128): SSH host trust failed")
        except SourceError:
            logger.exception("Source preparation failed")
    finally:
        stop_logging()
    content = path.read_text()
    assert "ValueError" in content and "test_diagnostics.py:" in content
    assert "secret-token" not in content and "private PR title" not in content
    assert "Native review requires a configured local repository path" in content
    assert "Git fetch failed (exit 128): SSH host trust failed" in content
    assert not capsys.readouterr().err
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_rotation_and_reconfiguration() -> None:
    path = configure_logging()
    configure_logging()
    logger = logging.getLogger("diffbutler.test")
    handlers = [
        h for h in logging.getLogger("diffbutler").handlers if isinstance(h, DiagnosticHandler)
    ]
    assert len(handlers) == 1
    assert handlers[0].maxBytes == 5 * 1024 * 1024 and handlers[0].backupCount == 3
    handlers[0].maxBytes = 150
    try:
        for index in range(12):
            logger.info("Synthetic diagnostic event %d", index)
    finally:
        stop_logging()
    assert path.exists()
    assert len(list(path.parent.glob("diffbutler.log.*"))) == 3


def test_default_state_location(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert state_path() == tmp_path / ".local/state/diffbutler"


def test_cli_logs_failure_and_reset_removes_logs(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[broken")
    with pytest.raises(SystemExit):
        main(["sync", "--config", str(invalid)])
    assert "ConfigError" in log_path().read_text()
    assert "CLI operation failed" in log_path().read_text()
    main(["reset", "--force"])
    assert not state_path().exists()


def test_debug_logging_is_opt_in_and_reconfiguration_resets_level() -> None:
    logger = logging.getLogger("diffbutler.test")
    path = configure_logging()
    logger.debug("hidden before opt-in")
    configure_logging(level=logging.DEBUG)
    logger.debug("visible debug event")
    configure_logging()
    logger.debug("hidden after opt-out")
    stop_logging()
    content = path.read_text()
    assert "Debug diagnostics enabled version=" in content
    assert "visible debug event" in content
    assert "hidden before opt-in" not in content
    assert "hidden after opt-out" not in content


@pytest.mark.parametrize(
    "level,expected",
    [
        ("debug", logging.DEBUG),
        ("DEBUG", logging.DEBUG),
        ("warning", logging.WARNING),
        ("error", logging.ERROR),
        ("critical", logging.CRITICAL),
        ("info", logging.INFO),
    ],
)
def test_cli_log_level_option(level: str, expected: int) -> None:
    main(["--log-level", level, "config"])
    assert logging.getLogger("diffbutler").level == expected
    assert ("Debug diagnostics enabled" in log_path().read_text()) == (expected == logging.DEBUG)


def test_cli_rejects_unknown_log_level() -> None:
    with pytest.raises(SystemExit) as error:
        main(["--log-level", "verbose", "config"])
    assert error.value.code == 2
