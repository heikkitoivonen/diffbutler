"""Conservative credential heuristics, not a guarantee that source has no secrets."""

import math
import re
from collections import Counter
from dataclasses import dataclass

# Prefix matches are masked even for low-entropy example credentials. Assignment
# matches require entropy to avoid hiding ordinary configuration and prose.
_PREFIXED = re.compile(
    rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    rb"|(?:AKIA|ASIA)[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{20,}"
    rb"|xox[baprs]-[A-Za-z0-9-]{20,})"
)
_ASSIGNMENT = re.compile(
    rb"(?i)(?<![A-Za-z0-9_])"
    rb"[A-Za-z0-9_]*(?:token|password|passwd|secret|api[_-]?key|access[_-]?key)"
    rb"[A-Za-z0-9_]*[\"']?[ \t]*[:=][ \t]*[\"']?"
    rb"(?P<value>[A-Za-z0-9_./+=~-]{16,})"
)
_PRIVATE_KEY = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    rb"[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----"
)


@dataclass(frozen=True)
class RedactedSource:
    content: bytes
    count: int


def redact_source(content: bytes) -> RedactedSource:
    """Mask matching spans without changing byte offsets or CR/LF positions.

    Counts describe merged occurrences, not unique credentials. No original value,
    hash, or source location is included in diagnostics. Works on arbitrary bytes.
    """
    spans = [
        match.span() for pattern in (_PREFIXED, _PRIVATE_KEY) for match in pattern.finditer(content)
    ]
    for match in _ASSIGNMENT.finditer(content):
        value = match.group("value")
        entropy = -sum(
            (count / len(value)) * math.log2(count / len(value))
            for count in Counter(value).values()
        )
        if entropy >= 3.5:
            spans.append(match.span("value"))
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
