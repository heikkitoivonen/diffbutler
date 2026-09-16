"""Check the installed CLI's public scaffold behavior."""

from importlib.metadata import version

import pytest

from diffbutler.ui.cli import main


def test_no_arguments_explains_project_status(capsys: pytest.CaptureFixture[str]) -> None:
    main([])
    output = capsys.readouterr()
    assert "not implemented yet" in output.out
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


def test_unimplemented_commands_are_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["review", "1841"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
