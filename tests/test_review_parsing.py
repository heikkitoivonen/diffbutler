"""Synthetic output only; no backend invocation or forge access."""

import json

import pytest

from diffbutler.reviewers.base import ReviewOutput
from diffbutler.reviewers.parsing import parse_review


def payload() -> dict[str, object]:
    return {
        "orientation": {
            "intent": "Reject expired sessions.",
            "shape": "One guard and its callers.",
            "look_here": [{"path": "src/auth.py", "line": 12, "why": "Check the guard."}],
            "low_risk": ["Renamed helper"],
            "unverified": ["Tests were read, not executed."],
        },
        "findings": [],
    }


def finding(**updates: object) -> dict[str, object]:
    return {
        "path": "src/auth.py",
        "line": 12,
        "start_line": 10,
        "side": "RIGHT",
        "severity": "important",
        "category": "correctness",
        "title": "Expired session accepted",
        "body": 'A quoted brace: "}" and backslash: \\ do not end the object.',
        "confidence": 0.8,
        **updates,
    }


def output(text: str, *, exit_code: int = 0) -> ReviewOutput:
    return ReviewOutput(text.encode(), b"synthetic stderr", exit_code, "synthetic-session")


@pytest.mark.parametrize(
    ("template", "tier"),
    [
        ("{}", "json"),
        ("Notes\n```json\n{}\n```\n", "fence"),
        ("Notes before {} notes after", "object"),
    ],
)
def test_tiers_preserve_orientation_and_findings(template: str, tier: str) -> None:
    document = payload()
    document["findings"] = [finding(), finding(confidence=0.1, severity="nit")]
    captured = output(template.format(json.dumps(document)))
    parsed = parse_review(captured)
    assert parsed.output is captured
    assert parsed.tier == tier
    assert parsed.error is None
    assert parsed.document is not None
    assert parsed.document.orientation.unverified == ("Tests were read, not executed.",)
    assert len(parsed.document.findings) == 2  # No confidence or severity filtering loss.
    assert parsed.document.findings[1].confidence == 0.1
    with pytest.raises(ValueError):
        parsed.document.orientation.intent = "changed"


def test_clean_pass_requires_orientation() -> None:
    valid = parse_review(output(json.dumps(payload())))
    assert valid.document is not None
    assert valid.document.findings == ()
    assert parse_review(output('{"findings": []}')).error


def test_unknown_labels_are_normalized_with_safe_notes() -> None:
    document = payload()
    document["findings"] = [finding(severity="private-label", category="secret-category")]
    result = parse_review(output(json.dumps(document)))
    assert result.document is not None
    assert result.document.findings[0].severity == "minor"
    assert result.document.findings[0].category == "style"
    assert len(result.notes) == 2
    assert "private-label" not in repr(result.notes)
    assert "secret-category" not in repr(result.notes)


@pytest.mark.parametrize(
    "updates",
    [
        {"line": True},
        {"line": "12"},
        {"line": 0},
        {"line": -1},
        {"start_line": 13},
        {"path": "../secret"},
        {"path": "/secret"},
        {"path": "src//auth.py"},
        {"side": "HEAD"},
        {"title": "  "},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"confidence": "0.8"},
        {"confidence": float("nan")},
        {"severity": None},
        {"category": 42},
        {"posted": True},
    ],
)
def test_invalid_finding_fails_whole_document(updates: dict[str, object]) -> None:
    document = payload()
    document["findings"] = [finding(), finding(**updates)]
    captured = output(json.dumps(document))
    result = parse_review(captured)
    assert result.document is None
    assert result.error == "invalid review document"
    assert result.output is captured


@pytest.mark.parametrize("text", ["", "No issues.", "{}", "[]", "null", '{"orientation":'])
def test_invalid_output_is_never_clean(text: str) -> None:
    assert parse_review(output(text)).document is None


def test_last_fence_wins_and_invalid_last_fence_does_not_recover_earlier_clean() -> None:
    clean = json.dumps(payload())
    document = payload()
    document["findings"] = [finding()]
    text = f"```json\n{clean}\n```\n```json\n{json.dumps(document)}\n```"
    result = parse_review(output(text))
    assert result.document is not None
    assert len(result.document.findings) == 1
    for last in ('{"findings":', '{"findings": []}'):
        result = parse_review(output(f"```json\n{clean}\n```\n```json\n{last}\n```"))
        assert result.document is None


def test_duplicate_keys_and_nonfinite_numbers_fail_without_recovery() -> None:
    clean = json.dumps(payload())
    duplicate = clean[:-1] + ', "findings": []}'
    assert parse_review(output(duplicate)).error
    assert parse_review(output(f"```json\n{duplicate}\n```")).error
    assert parse_review(output(clean[:-1] + ', "usage": Infinity}')).error


def test_exit_encoding_and_large_output() -> None:
    captured = output(json.dumps(payload()), exit_code=1)
    assert parse_review(captured).error == (
        "reviewer exited unsuccessfully (exit code 1); inspect captured output"
    )
    assert parse_review(ReviewOutput(b"\xff", b"", 0)).error == "output is not UTF-8"
    captured = output(json.dumps(payload()))
    assert parse_review(ReviewOutput(captured.stdout, b"x" * 4_000_001, 0)).document is not None


def test_deeply_nested_output_fails_safely() -> None:
    assert parse_review(output("[" * 2000 + "]" * 2000)).error


def test_empty_suggestion_can_represent_deletion() -> None:
    document = payload()
    document["findings"] = [finding(suggestion="")]
    parsed = parse_review(output(json.dumps(document)))
    assert parsed.document is not None
    assert parsed.document.findings[0].suggestion == ""


def test_crlf_fences_and_unfinished_final_fence() -> None:
    clean = json.dumps(payload())
    assert parse_review(output(f"```json\r\n{clean}\r\n```\r\n")).tier == "fence"
    assert parse_review(output(f"```json\n{clean}\n```\n```json\n{clean}")).error
    assert parse_review(output("```json\n" * 10_000)).error
