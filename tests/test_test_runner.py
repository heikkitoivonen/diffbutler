"""The two test tracks preserve failures, filters, and coverage across subprocesses."""

import subprocess
from unittest.mock import Mock

import pytest
from scripts.run_tests import run


@pytest.mark.parametrize(
    ("codes", "expected"),
    [([0, 0], 0), ([5, 0], 0), ([0, 5], 0), ([5, 5], 5), ([1, 0], 1), ([0, 1], 1), ([2], 2)],
)
def test_tracks_preserve_exit_status(
    monkeypatch: pytest.MonkeyPatch, codes: list[int], expected: int
) -> None:
    process = Mock(side_effect=[subprocess.CompletedProcess([], code) for code in codes])
    monkeypatch.setattr("scripts.run_tests.subprocess.run", process)
    assert run([]) == expected
    assert process.call_count == len(codes)


@pytest.mark.parametrize("marker_option", ["-m", "--markexpr"])
def test_filters_and_workers_apply_to_both_tracks(
    monkeypatch: pytest.MonkeyPatch, marker_option: str
) -> None:
    process = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr("scripts.run_tests.subprocess.run", process)
    assert (
        run(["-n", "2", marker_option, "slow or database", "-k", "review", "tests/test_native.py"])
        == 0
    )
    parallel = process.call_args_list[0].args[0]
    serial = process.call_args_list[1].args[0]
    assert parallel[-2:] == ["-m", "(slow or database) and (not serial)"]
    assert serial[-4:] == ["-m", "(slow or database) and (serial)", "-n", "0"]
    for command in (parallel, serial):
        assert command[command.index("-k") + 1] == "review"
        assert "tests/test_native.py" in command


def test_coverage_appends_before_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr("scripts.run_tests.subprocess.run", process)
    assert run(["--coverage"]) == 0
    commands = [call.args[0] for call in process.call_args_list]
    assert "--cov" in commands[0] and "--cov-append" not in commands[0]
    assert "--cov-append" in commands[1]
    assert [command[-1] for command in commands[2:]] == ["report", "html", "xml"]
