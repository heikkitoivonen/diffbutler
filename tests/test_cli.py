"""Check the installed CLI's public scaffold behavior."""

from importlib.metadata import version
from pathlib import Path

import pytest

from diffbutler.config.settings import config_path, load_settings
from diffbutler.ui.cli import main


def test_no_arguments_explains_project_status(capsys: pytest.CaptureFixture[str]) -> None:
    main([])
    output = capsys.readouterr()
    assert "Run diffbutler dashboard" in " ".join(output.out.split())
    assert output.err == ""


@pytest.mark.parametrize("argument", ["-h", "--help", "-v", "--version"])
def test_information_options(argument: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main([argument])
    assert error.value.code == 0
    output = capsys.readouterr().out
    if argument in {"-v", "--version"}:
        assert output.strip() == f"diffbutler {version('diffbutler')}"
    else:
        assert "--version" in output


@pytest.mark.parametrize("command", ["config", "reset", "doctor", "review", "dashboard", "ls"])
def test_short_help_matches_long_help(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    outputs: list[str] = []
    for flag in ("-h", "--help"):
        with pytest.raises(SystemExit) as error:
            main([command, flag])
        assert error.value.code == 0
        output = capsys.readouterr()
        assert output.err == ""
        outputs.append(output.out)
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("custom", [False, True])
def test_config_creates_defaults_and_shows_path(
    custom: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = load_settings().model_dump()
    monkeypatch.setenv("DIFFBUTLER_GENERAL__POLL_INTERVAL_S", "999")
    monkeypatch.setenv("DIFFBUTLER_REVIEWERS", "invalid-json")
    path = tmp_path / "custom" / "settings.toml" if custom else config_path()
    arguments = ["config", "--config", str(path)] if custom else ["config"]
    main(arguments)
    content = path.read_text()
    output = capsys.readouterr()
    assert output.out == content + f"\nConfig file: {path}\n"
    assert output.err == ""
    monkeypatch.delenv("DIFFBUTLER_GENERAL__POLL_INTERVAL_S")
    monkeypatch.delenv("DIFFBUTLER_REVIEWERS")
    assert load_settings(path).model_dump() == expected
    assert not (tmp_path / "data").exists()
    if custom:
        assert not config_path().exists()


@pytest.mark.parametrize(
    "content", ["", "# Keep my comments\n[general]\npoll_interval_s = 42\n", "[broken"]
)
def test_config_shows_existing_contents_without_rewriting(
    content: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(content)
    before = path.stat().st_mtime_ns
    main(["config", "--config", str(path)])
    output = capsys.readouterr()
    separator = "\n" if not content or content.endswith("\n") else "\n\n"
    assert output.out == content + separator + f"Config file: {path}\n"
    assert output.err == ""
    assert path.read_text() == content
    assert path.stat().st_mtime_ns == before


def test_config_prints_absolute_path_for_relative_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Relative custom paths are resolved against the current working directory.
    monkeypatch.chdir(tmp_path)
    main(["config", "--config", "nested/settings.toml"])
    assert capsys.readouterr().out.endswith(f"Config file: {tmp_path}/nested/settings.toml\n")


def test_config_reports_file_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "directory"
    path.mkdir()
    with pytest.raises(SystemExit) as error:
        main(["config", "--config", str(path)])
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "could not read or create configuration file" in output.err
    assert str(path) in output.err
    assert output.out == ""


def test_review_requires_a_persisted_qualified_pr(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["review", "1841"])
    assert error.value.code == 1
    assert "PR is missing or ambiguous" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ["ls", "--forge", "2"],
        ["ls", "--view", "all", "--login", "reader"],
        ["ls", "--repo", "acme/api"],
        ["ls", "--view", "all", "--repo", "invalid"],
    ],
)
def test_inapplicable_filters_fail_before_remote_access(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2


def test_cleanup_failure_is_visible_without_claiming_deletion(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    from diffbutler.domain.lifecycle import CleanupStatus

    async def failed_cleanup(path: Path | None) -> CleanupStatus:
        return CleanupStatus(pending=1, running=1, failed=1)

    monkeypatch.setattr("diffbutler.ui.cli.cleanup_configured", failed_cleanup)
    with pytest.raises(SystemExit) as error:
        main(["cleanup"])
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "0 succeeded, 1 pending, 1 running, 1 failed" in output.out
    assert "resources retained for retry" in output.err
