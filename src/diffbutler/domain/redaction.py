"""Conservative credential heuristics, not a guarantee that source has no secrets."""

import math
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

# Prefix matches are masked even for low-entropy example credentials. Assignment
# matches require entropy to avoid hiding ordinary configuration and prose.
_PREFIXED = re.compile(
    rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    rb"|(?:AKIA|ASIA)[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{20,}"
    rb"|xox[baprs]-[A-Za-z0-9-]{20,})"
)
# Assignment names may join two runs with a hyphen only inside api-key or
# access-key. Names are scanned possessively and checked in code so adversarial
# word runs stay linear; a regex alternative inside open-ended runs backtracks.
_NAME = re.compile(rb"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]++)[\"']?[ \t]*[:=][ \t]*[\"']?")
_KEYWORD = re.compile(rb"(?i)token|password|passwd|secret|api[_-]?key|access[_-]?key")
_HYPHEN_PREFIX = re.compile(rb"(?i)(?:api|access)\Z")
_HYPHEN_SUFFIX = re.compile(rb"(?i)key")
_VALUE = re.compile(rb"[A-Za-z0-9_./+=~-]{16,}")
_KEY_BEGIN = re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_KEY_END = re.compile(rb"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----")


def _credential_name(name: bytes) -> bool:
    """Whether a keyword sits in the final hyphen-free run, or spans api-key/access-key."""
    *runs, last = name.split(b"-")
    if _KEYWORD.search(last):
        return True
    return bool(runs and _HYPHEN_PREFIX.search(runs[-1]) and _HYPHEN_SUFFIX.match(last))


def _assignment_values(content: bytes) -> Iterator[tuple[int, int]]:
    position = 0
    while (name := _NAME.search(content, position)) is not None:
        if _credential_name(name[1]) and (value := _VALUE.match(content, name.end())):
            yield value.span()
            position = value.end()
        else:
            # Values and separators may contain further assignments.
            position = name.end(1)


def _private_keys(content: bytes) -> Iterator[tuple[int, int]]:
    position = 0
    while (begin := _KEY_BEGIN.search(content, position)) is not None:
        end = _KEY_END.search(content, begin.end())
        if end is None:
            return  # No later END can close a later BEGIN either.
        yield begin.start(), end.end()
        position = end.end()


@dataclass(frozen=True)
class RedactedSource:
    content: bytes
    count: int


def redact_source(content: bytes) -> RedactedSource:
    """Mask matching spans without changing byte offsets or CR/LF positions.

    Counts describe merged occurrences, not unique credentials. No original value,
    hash, or source location is included in diagnostics. Works on arbitrary bytes.
    """
    spans = [match.span() for match in _PREFIXED.finditer(content)]
    spans.extend(_private_keys(content))
    for start, end in _assignment_values(content):
        value = content[start:end]
        entropy = -sum(
            (count / len(value)) * math.log2(count / len(value))
            for count in Counter(value).values()
        )
        if entropy >= 3.5:
            spans.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    result = bytearray(content)
    for start, end in merged:
        for index in range(start, end):
            if result[index] not in (10, 13):
                result[index] = ord("*")
    return RedactedSource(bytes(result), len(merged))
