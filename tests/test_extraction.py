"""Synthetic protocols only; no paid backends, credentials, or assumed CLI flags."""

import json

import pytest
from tests.test_review_parsing import payload

from diffbutler.reviewers.base import ReviewOutput
from diffbutler.reviewers.extraction import EnvelopeFormat, StreamFormat, extract_output
from diffbutler.reviewers.parsing import parse_review


def capture(*events: dict[str, object]) -> ReviewOutput:
    return ReviewOutput(b"\n".join(json.dumps(event).encode() for event in events), b"stderr", 0)


def test_nested_envelope_and_raw_capture_are_preserved() -> None:
    output = capture({"data": {"answer": json.dumps(payload()), "session": "opaque; $(id)"}})
    format = EnvelopeFormat(text_path=("data", "answer"), session_path=("data", "session"))
    parsed = parse_review(output, output_format=format)
    assert parsed.document is not None and parsed.error is None
    assert parsed.output.stdout == output.stdout and parsed.output.stderr == b"stderr"
    assert parsed.output.backend_session_id == "opaque; $(id)"


def test_last_complete_assistant_wins_not_tool_output_or_deltas() -> None:
    output = capture(
        {"type": "session", "id": "conversation"},
        {"type": "assistant", "text": "earlier invalid review"},
        {"type": "delta", "text": "invalid fragment"},
        {"type": "assistant", "text": json.dumps(payload())},
        {"type": "tool", "text": "arbitrary final tool output"},
        {"type": "complete"},
    )
    parsed = parse_review(output, output_format=StreamFormat())
    assert parsed.document is not None and parsed.output.stdout == output.stdout
    assert parsed.output.backend_session_id == "conversation"


@pytest.mark.parametrize(
    "tail",
    [
        (),
        ({"type": "error", "message": "private"},),
        ({"type": "assistant", "text": "invalid final answer"}, {"type": "complete"}),
        ({"type": "assistant", "text": None}, {"type": "complete"}),
        ({"type": "session", "id": "other"}, {"type": "complete"}),
        ({"type": "complete"}, {"type": "assistant", "text": "late"}),
    ],
)
def test_invalid_final_events_never_fall_back_to_clean_pass(
    tail: tuple[dict[str, object], ...],
) -> None:
    output = capture(
        {"type": "session", "id": "first"},
        {"type": "assistant", "text": json.dumps(payload())},
        *tail,
    )
    parsed = parse_review(output, output_format=StreamFormat())
    assert parsed.document is None and parsed.error is not None
    assert parsed.output.stdout == output.stdout
    assert "private" not in parsed.error


@pytest.mark.parametrize(
    "raw",
    [
        b'{"result":"first","result":"second"}',
        b'{"result":NaN}',
        b"[]",
        b"null",
        b"\xff",
        b'{"result":{}}',
        b'{"result":"ok","is_error":true}',
        b'{"result":"ok","session_id":""}',
        b'{"result":"ok","session_id":"a\\nb"}',
        b'{"result":"ok","session_id":42}',
        b'{"result":',
    ],
)
def test_bad_envelope_retains_bytes(raw: bytes) -> None:
    output = ReviewOutput(raw, b"private", 0)
    extracted = extract_output(output, EnvelopeFormat())
    assert extracted.error == "invalid backend output" and extracted.text is None
    assert extracted.output is output


@pytest.mark.parametrize(
    "suffix",
    [
        b"\n{",
        b'\n{"type":"complete","type":"error"}',
        b"\nnull",
        b'\n{"type":NaN}',
        b'\n{"unexpected":42}',
    ],
)
def test_malformed_stream_records_fail(suffix: bytes) -> None:
    good = capture({"type": "assistant", "text": json.dumps(payload())})
    output = ReviewOutput(good.stdout + suffix, b"", 0)
    assert extract_output(output, StreamFormat()).error is not None


def test_nonzero_overflow_and_identity_conflict() -> None:
    output = capture({"result": json.dumps(payload()), "session_id": "one"})
    assert extract_output(output, EnvelopeFormat(), max_output_bytes=10).error == (
        "output exceeds byte budget"
    )
    assert (
        parse_review(ReviewOutput(output.stdout, b"", 2), output_format=EnvelopeFormat()).document
        is None
    )
    conflict = ReviewOutput(output.stdout, b"", 0, "two")
    assert extract_output(conflict, EnvelopeFormat()).error == "invalid backend output"
    with pytest.raises(ValueError):
        StreamFormat(complete_type="assistant")
