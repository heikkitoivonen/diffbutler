"""Synthetic credentials only; redaction never consults environment or network."""

import logging
import math
import random
import re
import time
from collections import Counter

import pytest
from tests.test_context import FakeSources, records

from diffbutler.core.context import assemble_context
from diffbutler.domain.redaction import redact_source
from diffbutler.domain.review import Side, SourceFile
from diffbutler.reviewers.base import ReviewBudget
from diffbutler.reviewers.redaction import RedactingSourceReader

TOKEN = b"ghp_0123456789abcdefghijklmnopqrstuv"


@pytest.mark.parametrize(
    "secret",
    [
        TOKEN,
        b"github_pat_0123456789abcdefghijklmnop",
        b"AKIA0123456789ABCDEF",
        b"sk-proj-0123456789abcdefghijklmnop",
        b"xoxb-0123456789abcdefghijklmnop",
    ],
)
def test_prefixed_credentials_masked_without_changing_offsets(secret: bytes) -> None:
    original = b"+value = '" + secret + b"'\r\n unchanged\n"
    result = redact_source(original)
    assert result.content == original.replace(secret, b"*" * len(secret))
    assert result.count == 1
    assert redact_source(result.content).count == 0


def test_assignments_entropy_overlap_and_repeated_occurrences() -> None:
    secret = b"aB3dE5gH7jK9mN2pQ4sT6vW8"
    original = b'"api_key": "' + secret + b'"\nPASSWORD=' + secret + b"\ntoken=" + TOKEN
    result = redact_source(original)
    assert secret not in result.content and TOKEN not in result.content
    assert result.count == 3  # Prefix and assignment detections overlap.
    ordinary = b'token="aaaaaaaaaaaaaaaaaaaaaaaa"\nchecksum=0123456789abcdef0123456789abcdef'
    assert redact_source(ordinary).content == ordinary


def test_private_key_and_binary_bytes_preserve_line_positions() -> None:
    original = (
        b"\xff\x00before\r\n-----BEGIN RSA PRIVATE KEY-----\r\n"
        b"+SyntheticKeyMaterial0123456789\r\n-----END RSA PRIVATE KEY-----\nafter"
    )
    result = redact_source(original)
    assert result.count == 1 and b"Synthetic" not in result.content
    assert result.content.startswith(b"\xff\x00before\r\n") and result.content.endswith(b"\nafter")
    assert len(result.content) == len(original)
    assert [(i, c) for i, c in enumerate(original) if c in (10, 13)] == [
        (i, c) for i, c in enumerate(result.content) if c in (10, 13)
    ]


@pytest.mark.parametrize(
    ("line", "name"),
    [
        (b"x: api_key=", b"api_key"),
        (b"foo=bar_token=", b"bar_token"),
        (b"my_access-key_id: ", b"my_access-key_id"),
        (b"config-api-key = ", b"config-api-key"),
        (b"--token ", None),
        (b"apikey-foo=", None),
        (b"token-: ", None),
    ],
)
def test_assignment_names_including_hyphens_and_nested_values(
    line: bytes, name: bytes | None
) -> None:
    secret = b"aB3dE5gH7jK9mN2pQ4sT6vW8"
    result = redact_source(line + secret)
    assert (secret not in result.content) == (name is not None)


# The original single-regex rules, kept as the reference for linear rewrites.
_REFERENCE_ASSIGNMENT = re.compile(
    rb"(?i)(?<![A-Za-z0-9_])"
    rb"[A-Za-z0-9_]*(?:token|password|passwd|secret|api[_-]?key|access[_-]?key)"
    rb"[A-Za-z0-9_]*[\"']?[ \t]*[:=][ \t]*[\"']?"
    rb"(?P<value>[A-Za-z0-9_./+=~-]{16,})"
)
_REFERENCE_PRIVATE_KEY = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    rb"[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----"
)


def reference_spans(content: bytes) -> list[tuple[int, int]]:
    spans = [match.span() for match in _REFERENCE_PRIVATE_KEY.finditer(content)]
    for match in _REFERENCE_ASSIGNMENT.finditer(content):
        value = match["value"]
        counts = Counter(value).values()
        if -sum(c / len(value) * math.log2(c / len(value)) for c in counts) >= 3.5:
            spans.append(match.span("value"))
    return spans


def test_linear_matching_masks_exactly_what_the_reference_rules_mask() -> None:
    pieces = [
        b"token", b"TOKEN", b"api", b"-key", b"key", b"access", b"_", b"-", b"x", b"password",
        b"secret", b":", b"=", b" ", b"\t", b'"', b"'", b"A9b8C7d6E5f4G3h2", b"Zq/+.~=-_xY",
        b"\n", b"api_key", b"-----BEGIN PRIVATE KEY-----", b"-----END PRIVATE KEY-----",
    ]  # fmt: skip
    generator = random.Random(0)
    for _ in range(20_000):
        content = b"".join(generator.choice(pieces) for _ in range(generator.randint(1, 14)))
        expected = bytearray(content)
        for start, end in reference_spans(content):
            expected[start:end] = bytes(b if b in (10, 13) else 42 for b in content[start:end])
        assert redact_source(content).content == bytes(expected), content


@pytest.mark.parametrize(
    "content",
    [
        b"+" + b"token" * 40_000 + b"\n",
        b"-----BEGIN PRIVATE KEY-----\n" * 20_000,
        b"a-" * 100_000 + b": value",
    ],
)
def test_adversarial_lines_redact_in_linear_time(content: bytes) -> None:
    # The former backtracking rules took minutes on these 200 KB inputs.
    started = time.perf_counter()
    redact_source(content)
    assert time.perf_counter() - started < 2


class SecretSources(FakeSources):
    async def read_file(self, source: SourceFile) -> bytes | None:
        if source.path == "missing":
            return None
        return b"\xff\x00" + TOKEN


@pytest.mark.asyncio
async def test_context_redacts_diff_and_every_file_read_with_safe_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run, pr = records()
    sources = SecretSources(
        "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+token=" + TOKEN.decode()
    )
    caplog.set_level(logging.INFO, logger="diffbutler.reviewers.redaction")
    context = await assemble_context(
        run=run,
        pr=pr,
        sources=sources,
        instructions="trusted",
        schema_json="{}",
        budget=ReviewBudget(),
    )
    assert TOKEN.decode() not in context.diff
    for side in Side:
        assert await context.sources.read_file(SourceFile("surrounding.py", side)) == (
            b"\xff\x00" + b"*" * len(TOKEN)
        )
    assert await context.sources.read_file(SourceFile("missing", Side.HEAD)) is None
    assert TOKEN.decode() not in caplog.text
    assert "surrounding.py" not in caplog.text
    assert [record.getMessage() for record in caplog.records] == [
        "Source redaction: 1 credential occurrences masked"
    ] * 3
    assert sources.diff.endswith(TOKEN.decode())  # The underlying source is untouched.


@pytest.mark.asyncio
async def test_reader_does_not_turn_failures_into_missing_source() -> None:
    class BrokenSources(FakeSources):
        async def read_file(self, source: SourceFile) -> bytes | None:
            raise OSError("synthetic failure")

    with pytest.raises(OSError, match="synthetic failure"):
        await RedactingSourceReader(BrokenSources()).read_file(SourceFile("a", Side.BASE))
