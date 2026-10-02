"""Redact every mediated read before exposing bytes to a review adapter."""

import logging

from diffbutler.domain.redaction import redact_source
from diffbutler.domain.review import SourceFile
from diffbutler.reviewers.base import SourceReader

_LOGGER = logging.getLogger(__name__)


def redact_and_report(content: bytes) -> bytes:
    result = redact_source(content)
    _LOGGER.log(
        logging.INFO if result.count else logging.DEBUG,
        "Source redaction: %d credential occurrences masked",
        result.count,
    )
    return result.content


class RedactingSourceReader:
    """Internal mediation only; Python object encapsulation is not a sandbox."""

    def __init__(self, reader: SourceReader) -> None:
        self._reader = reader

    async def read_file(self, source: SourceFile) -> bytes | None:
        content = await self._reader.read_file(source)
        return None if content is None else redact_and_report(content)
