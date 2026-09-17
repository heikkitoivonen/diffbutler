"""Synthetic credentials only; redaction never consults environment or network."""

import logging

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
    sources = SecretSources("+token=" + TOKEN.decode())
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
