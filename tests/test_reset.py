"""Reset confirms deletion and stays within isolated application directories."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from diffbutler.config.settings import config_path, data_path
from diffbutler.ui.cli import app


def populate() -> tuple[Path, Path]:
    config = config_path().parent
    data = data_path()
    for root in (config, data):
        (root / "nested").mkdir(parents=True, exist_ok=True)
        (root / "nested" / "session.jsonl").write_text("session")
        (root / "diffbutler.db").write_bytes(b"database")
        (root / "diffbutler.db-wal").write_bytes(b"journal")
        (root / ".hidden").write_text("hidden")
    config_path().write_text("[invalid configuration")
    return config, data


@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_reset_declined_or_eof_preserves_everything(answer: str) -> None:
    roots = populate()
    before = {
        path: path.read_bytes() for root in roots for path in root.rglob("*") if path.is_file()
    }
    result = CliRunner().invoke(app, ["reset"], input=answer)
    assert result.exit_code != 0
    assert "Are you sure? [y/N]" in result.output
    for path, content in before.items():
        assert path.read_bytes() == content


@pytest.mark.parametrize("force", [False, True])
def test_reset_removes_all_contents_and_preserves_other_directories(
    force: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = populate()
    outside = tmp_path / "custom.db"
    outside.write_text("preserve")
    neighbors = [root.parent / "another-app" for root in roots]
    for path in neighbors:
        path.mkdir()
        (path / "keep").write_text("preserve")
    monkeypatch.setenv("DIFFBUTLER_DB__URL", f"sqlite+aiosqlite:///{outside}")
    monkeypatch.setenv("DIFFBUTLER_REVIEWERS", "invalid-json")
    result = CliRunner().invoke(
        app, ["reset", *(["--force"] if force else [])], input="" if force else "y\n"
    )
    assert result.exit_code == 0, result.output
    assert ("Are you sure?" in result.output) is not force
    assert all(str(root) in result.output and not root.exists() for root in roots)
    assert outside.read_text() == "preserve"
    assert all((path / "keep").read_text() == "preserve" for path in neighbors)


def test_reset_missing_directories_is_idempotent() -> None:
    for _ in range(2):
        result = CliRunner().invoke(app, ["reset", "--force"])
        assert result.exit_code == 0, result.output
    assert not config_path().parent.exists()
    assert not data_path().exists()


@pytest.mark.parametrize("root_link", [False, True])
def test_reset_does_not_follow_symlinks(root_link: bool, tmp_path: Path) -> None:
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "keep").write_text("preserve")
    root = config_path().parent
    root.parent.mkdir(parents=True)
    if root_link:
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        (root / "linked").symlink_to(outside, target_is_directory=True)
        (root / "broken").symlink_to(tmp_path / "missing")
    result = CliRunner().invoke(app, ["reset", "--force"])
    assert result.exit_code == 0, result.output
    assert not root.exists() and not root.is_symlink()
    assert (outside / "keep").read_text() == "preserve"


def test_reset_handles_shared_config_and_data_root(monkeypatch: pytest.MonkeyPatch) -> None:
    roots = populate()
    monkeypatch.setenv("XDG_DATA_HOME", str(roots[0].parent))
    result = CliRunner().invoke(app, ["reset", "--force"])
    assert result.exit_code == 0, result.output
    assert not roots[0].exists()
    assert roots[1].exists()


def test_reset_reports_deletion_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    roots = populate()

    def denied(path: Path) -> None:
        raise PermissionError("synthetic permission failure")

    monkeypatch.setattr("diffbutler.core.reset.shutil.rmtree", denied)
    result = CliRunner().invoke(app, ["reset", "--force"])
    assert result.exit_code == 1
    assert "could not remove" in result.output
    assert "configuration and data removed" not in result.output
    assert all(root.exists() for root in roots)
