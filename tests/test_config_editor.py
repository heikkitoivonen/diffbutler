"""Config editing uses an offline fake editor and literal argument passing."""

import shlex
import sys
from pathlib import Path

import pytest

from diffbutler.config.settings import config_path, load_settings
from diffbutler.ui.cli import main


@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_config_edit_waits_for_editor_and_preserves_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    custom: bool,
    existing: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "custom config $(touch injected).toml" if custom else config_path()
    original = "# Existing comments\n[general]\npoll_interval_s = 42\n"
    if existing:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(original)
    editor = tmp_path / "fake editor.py"
    editor.write_text(
        "import sys\nfrom pathlib import Path\n"
        "assert sys.argv[1] == 'argument with spaces'\n"
        "assert len(sys.argv) == 3\n"
        "path = Path(sys.argv[2])\n"
        "assert path.is_absolute() and path.is_file()\n"
        "path.write_text(path.read_text() + '# edited\\n')\n"
    )
    monkeypatch.setenv("EDITOR", shlex.join([sys.executable, str(editor), "argument with spaces"]))
    main(["config", "--edit", *(["--config", str(path)] if custom else [])])
    output = capsys.readouterr()
    assert output.out == f"\nConfig file: {path}\n"
    assert output.err == ""
    assert path.read_text().endswith("# edited\n")
    if existing:
        assert path.read_text() == original + "# edited\n"
    else:
        assert load_settings(path).general.poll_interval_s == 600
    assert not (tmp_path / "injected").exists()
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    ("editor", "message"),
    [
        (None, "Set EDITOR"),
        ("   ", "Set EDITOR"),
        ('"unterminated', "invalid quoting"),
        ("./nonexistent-editor", "Could not launch EDITOR"),
    ],
)
def test_config_edit_reports_editor_configuration_errors(
    editor: str | None,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EDITOR", raising=False)
    if editor is not None:
        monkeypatch.setenv("EDITOR", editor)
    with pytest.raises(SystemExit) as error:
        main(["config", "--edit"])
    assert error.value.code == 1
    assert message in capsys.readouterr().err


def test_config_edit_reports_unsuccessful_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EDITOR", shlex.join([sys.executable, "-c", "raise SystemExit(7)"]))
    with pytest.raises(SystemExit) as error:
        main(["config", "--edit"])
    assert error.value.code == 1
    assert "Editor exited with status 7" in capsys.readouterr().err
