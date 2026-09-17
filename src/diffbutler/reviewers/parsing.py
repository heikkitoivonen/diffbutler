"""Parse captured review text without executing a backend or changing queue state."""

import json
from dataclasses import dataclass
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from diffbutler.domain.review import Side, SourceFile
from diffbutler.reviewers.base import ReviewOutput

Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Line = Annotated[int, Field(gt=0)]
Severity = Literal["blocking", "important", "minor", "nit", "question"]
Category = Literal["correctness", "security", "performance", "api", "tests", "style", "docs"]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class Location(_Record):
    path: Text
    line: Line

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        SourceFile(value, Side.HEAD)
        return value


class LookHere(Location):
    why: Text


class Orientation(_Record):
    intent: Text
    shape: Text
    look_here: tuple[LookHere, ...]
    low_risk: tuple[Text, ...]
    unverified: tuple[Text, ...]


class Finding(Location):
    start_line: Line | None = None
    side: Literal["LEFT", "RIGHT"]
    severity: Severity
    category: Category
    title: Text
    body: Text
    suggestion: str | None = None
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered_range(self) -> Finding:
        if self.start_line is not None and self.start_line > self.line:
            raise ValueError("finding range is reversed")
        return self


class ReviewDocument(_Record):
    orientation: Orientation
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class ParsedReview:
    """Raw bytes remain available on failure; findings are never silently dropped.

    Locations are only structurally valid. DiffIndex validation is still required
    before any anchor can be considered commentable.
    """

    output: ReviewOutput
    document: ReviewDocument | None
    tier: Literal["json", "fence", "object"] | None
    notes: tuple[str, ...] = ()
    error: str | None = None


class _InvalidJSON(ValueError):
    pass


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidJSON("duplicate key")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise _InvalidJSON("nonfinite number")


def _load(text: str) -> object:
    return json.loads(text, object_pairs_hook=_object, parse_constant=_constant)


def _outer_object(text: str) -> str | None:
    """Extract the first outer object, respecting quoted braces and escapes."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    quoted = escaped = False
    for offset in range(start, len(text)):
        char = text[offset]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : offset + 1]
    return None


def _last_fence(text: str) -> str | None:
    # Scan once, including an unfinished final fence; never select an older answer.
    lines: list[str] | None = None
    candidate: str | None = None
    for line in text.splitlines(keepends=True):
        marker = line.rstrip(" \t\r\n")
        if marker == "```json":
            lines = []
        elif marker == "```" and lines is not None:
            candidate = "".join(lines)
            lines = None
        elif lines is not None:
            lines.append(line)
    if lines is not None:
        raise _InvalidJSON("unfinished final fence")
    return candidate


def _normalize(value: object) -> tuple[object, tuple[str, ...]]:
    notes: list[str] = []
    if isinstance(value, dict):
        root = cast(dict[str, object], value)
        findings = root.get("findings")
        if isinstance(findings, list):
            for index, item in enumerate(cast(list[object], findings)):
                if not isinstance(item, dict):
                    continue
                finding = cast(dict[str, object], item)
                for key, choices, fallback in (
                    ("severity", {"blocking", "important", "minor", "nit", "question"}, "minor"),
                    (
                        "category",
                        {"correctness", "security", "performance", "api", "tests", "style", "docs"},
                        "style",
                    ),
                ):
                    label = finding.get(key)
                    if isinstance(label, str) and label not in choices:
                        finding[key] = fallback
                        notes.append(f"finding {index + 1}: unknown {key} normalized to {fallback}")
    return cast(object, value), tuple(notes)


def parse_review(output: ReviewOutput, *, max_output_bytes: int = 4_000_000) -> ParsedReview:
    """Parse already-extracted assistant text, not backend envelopes or JSONL.

    Syntax recovery follows SPEC §7.3. Once JSON is decoded, schema errors fail
    the entire result: an invalid finding must never turn into a clean review.
    Diagnostics contain no backend text; raw stdout/stderr stay in the result.
    """
    if max_output_bytes < 1:
        raise ValueError("output budget must be positive")
    if len(output.stdout) + len(output.stderr) > max_output_bytes:
        return ParsedReview(output, None, None, error="output exceeds byte budget")
    if output.exit_code != 0:
        return ParsedReview(output, None, None, error="reviewer exited unsuccessfully")
    try:
        text = output.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return ParsedReview(output, None, None, error="output is not UTF-8")
    tier: Literal["json", "fence", "object"] = "json"
    try:
        try:
            value = _load(text)
        except json.JSONDecodeError:
            candidate = _last_fence(text)
            tier = "fence"
            try:
                value = _load(candidate) if candidate is not None else _load("")
            except json.JSONDecodeError:
                # Do not recover an earlier clean answer from a malformed final fence.
                candidate = _outer_object(candidate if candidate is not None else text)
                tier = "object"
                value = _load(candidate or "")
        value, notes = _normalize(value)
        document = ReviewDocument.model_validate_json(json.dumps(value))
    except ValueError, RecursionError:
        return ParsedReview(output, None, None, error="invalid review document")
    return ParsedReview(output, document, tier, notes)
