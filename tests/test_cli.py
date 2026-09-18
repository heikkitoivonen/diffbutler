"""Check the installed CLI's public scaffold behavior."""

from importlib.metadata import version

import pytest

from diffbutler.ui.cli import main


def test_no_arguments_explains_project_status(capsys: pytest.CaptureFixture[str]) -> None:
    main([])
    output = capsys.readouterr()
    assert "Run diffbutler dashboard" in " ".join(output.out.split())
    assert output.err == ""


@pytest.mark.parametrize("argument", ["--help", "--version"])
def test_information_options(argument: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main([argument])
    assert error.value.code == 0
    output = capsys.readouterr().out
    if argument == "--version":
        assert output.strip() == f"diffbutler {version('diffbutler')}"
    else:
        assert "--version" in output


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
    from pathlib import Path

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
