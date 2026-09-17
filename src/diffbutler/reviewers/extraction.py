"""Declarative offline extraction of backend envelopes; no backend flag assumptions."""

import json
from dataclasses import dataclass
from typing import cast

from diffbutler.reviewers.base import ReviewOutput


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ValueError("nonfinite JSON number")


def _load(text: str) -> object:
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)


def _at(value: object, path: tuple[str, ...]) -> object:
    for part in path:
        if not isinstance(value, dict):
            return None
        value = cast(dict[str, object], value).get(part)
    return value


@dataclass(frozen=True)
class EnvelopeFormat:
    text_path: tuple[str, ...] = ("result",)
    session_path: tuple[str, ...] | None = ("session_id",)
    error_path: tuple[str, ...] | None = ("is_error",)


@dataclass(frozen=True)
class StreamFormat:
    """Only complete assistant messages are selected, never deltas or tool output.

    Required successful terminal events distinguish a complete turn from a
    truncated stream. Selectors describe a verified backend protocol; these
    defaults are a synthetic testing protocol, not a Codex/Claude preset.
    """

    type_path: tuple[str, ...] = ("type",)
    assistant_type: str = "assistant"
    text_path: tuple[str, ...] = ("text",)
    session_type: str = "session"
    session_path: tuple[str, ...] = ("id",)
    complete_type: str = "complete"
    failure_types: tuple[str, ...] = ("error", "failed")

    def __post_init__(self) -> None:
        types = (self.assistant_type, self.session_type, self.complete_type, *self.failure_types)
        if not all(types) or len(types) != len(set(types)):
            raise ValueError("stream event types must be nonempty and distinct")


type OutputFormat = EnvelopeFormat | StreamFormat


@dataclass(frozen=True)
class ExtractedOutput:
    """Raw output is never replaced by extracted assistant text."""

    output: ReviewOutput
    text: bytes | None
    backend_session_id: str | None
    error: str | None = None


def _identity(value: object, previous: str | None) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 1024
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
        or previous not in (None, value)
    ):
        raise ValueError("invalid or conflicting session identity")
    return value


def extract_output(
    output: ReviewOutput, format: OutputFormat, *, max_output_bytes: int = 4_000_000
) -> ExtractedOutput:
    """Fail the entire capture on malformed/truncated/error events, retaining raw bytes.

    No progress text is returned: tool events and earlier messages can contain
    secrets. A final assistant message must still pass the findings parser.
    """
    if max_output_bytes < 1:
        raise ValueError("output budget must be positive")
    if len(output.stdout) + len(output.stderr) > max_output_bytes:
        return ExtractedOutput(output, None, None, "output exceeds byte budget")
    session_id = output.backend_session_id
    try:
        if session_id is not None:
            _identity(session_id, None)
        text = output.stdout.decode("utf-8")
        if isinstance(format, EnvelopeFormat):
            root = _load(text)
            if not isinstance(root, dict):
                raise ValueError
            root = cast(dict[str, object], root)
            answer = _at(root, format.text_path)
            if format.error_path is not None and _at(root, format.error_path) not in (None, False):
                raise ValueError
            if format.session_path is not None:
                value = _at(root, format.session_path)
                if value is not None:
                    session_id = _identity(value, session_id)
        else:
            answer: object = None
            complete = False
            for line in text.splitlines():
                if not line.strip():
                    continue
                if complete:
                    raise ValueError
                event = _load(line)
                kind = _at(event, format.type_path)
                if not isinstance(event, dict) or not isinstance(kind, str):
                    raise ValueError
                event = cast(dict[str, object], event)
                if kind in format.failure_types:
                    raise ValueError
                if kind == format.session_type:
                    session_id = _identity(_at(event, format.session_path), session_id)
                elif kind == format.assistant_type:
                    answer = _at(event, format.text_path)
                    if not isinstance(answer, str):
                        raise ValueError
                elif kind == format.complete_type:
                    complete = True
            if not complete:
                raise ValueError
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError
        if output.exit_code != 0:
            return ExtractedOutput(output, None, session_id, "reviewer exited unsuccessfully")
        return ExtractedOutput(output, answer.encode("utf-8"), session_id)
    except ValueError, RecursionError:
        return ExtractedOutput(output, None, None, "invalid backend output")
