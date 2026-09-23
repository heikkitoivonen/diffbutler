"""Application exclusion uses a kernel lock, not a stale PID or file-existence test."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from diffbutler.config.diagnostics import log_path
from diffbutler.config.instance import instance_lock_path, single_instance
from diffbutler.config.settings import ConfigError, config_path
from diffbutler.ui.cli import app


def test_reentrant_start_is_rejected_without_overwriting_owner() -> None:
    with single_instance():
        path = instance_lock_path()
        original = path.read_bytes()
        with (
            pytest.raises(ConfigError, match=f"already running.*PID {os.getpid()}"),
            single_instance(),
        ):
            pytest.fail("Acquired a second instance")
        assert path.read_bytes() == original
    assert path.exists()
    with single_instance():
        assert path.read_bytes() == original
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("contents", ["999999999\n", "not a pid", ""])
def test_unheld_pid_file_does_not_block_start(contents: str) -> None:
    path = instance_lock_path()
    path.parent.mkdir(parents=True)
    path.write_text(contents)
    with single_instance():
        assert path.read_text() == f"{os.getpid()}\n"


def test_instance_lock_releases_on_exception() -> None:
    with pytest.raises(RuntimeError), single_instance():
        raise RuntimeError("synthetic failure")
    with single_instance():
        pass


def test_other_process_cannot_start_and_crash_releases_lock() -> None:
    code = (
        "import os\n"
        "from diffbutler.config.instance import single_instance\n"
        "with single_instance():\n"
        "    os._exit(0)\n"
    )
    with single_instance():
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
        assert result.returncode != 0
        assert b"DiffButler is already running" in result.stderr
    # os._exit skips finally blocks, leaving a PID file but no kernel owner.
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    child_pid = instance_lock_path().read_text()
    with single_instance():
        assert instance_lock_path().read_text() != child_pid


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_instance_lock_rejects_unsafe_files(tmp_path: Path, kind: str) -> None:
    path = instance_lock_path()
    path.parent.mkdir(parents=True)
    target = tmp_path / "preserve"
    target.write_text("untouched")
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        path.hardlink_to(target)
    else:
        path.mkdir()
    with pytest.raises(ConfigError), single_instance():
        pytest.fail("Unsafe instance file accepted")
    assert target.read_text() == "untouched"


@pytest.mark.parametrize("command", [["config"], ["dashboard", "--offline"], ["reset", "--force"]])
def test_busy_cli_exits_before_logging_setup_or_reset(command: list[str]) -> None:
    config_path().parent.mkdir(parents=True)
    config_path().write_text("preserve")
    with single_instance():
        result = CliRunner().invoke(app, command)
    assert result.exit_code == 1
    assert "already running" in result.output
    assert config_path().read_text() == "preserve"
    assert not log_path().exists()


def test_reset_keeps_lock_inode_and_releases_it_after_exit() -> None:
    path = instance_lock_path()
    with single_instance():
        inode = path.stat().st_ino
    result = CliRunner().invoke(app, ["reset", "--force"])
    assert result.exit_code == 0, result.output
    assert path.stat().st_ino == inode
    with single_instance():
        assert path.stat().st_ino == inode


@pytest.mark.parametrize("argument", ["--help", "--version"])
def test_information_options_work_while_busy(argument: str) -> None:
    with single_instance():
        result = CliRunner().invoke(app, [argument])
    assert result.exit_code == 0
    assert "already running" not in result.output
