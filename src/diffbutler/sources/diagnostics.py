"""Opt-in Git diagnostics. Never log source stdout or arbitrary environment values."""

import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from uuid import uuid4

from diffbutler.domain.redaction import redact_source

_SECRET_NAME = re.compile(r"token|password|passwd|secret|credential|api.?key", re.I)
_USERINFO = re.compile(r"(\w+://)[^\s/]+@")
_QUERY = re.compile(r"(\w+://[^\s?'\"]+)\?[^\s'\"]*")
_ASSIGNMENT = re.compile(
    r"(?i)(\b[\w-]*(?:token|password|passwd|secret|credential|api[_-]?key)[\w-]*"
    r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_AUTHORIZATION = re.compile(r"(?im)(\b(?:proxy-)?authorization\s*:\s*)[^\r\n]+")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?"
    r"(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)"
)


def _redact(value: str) -> str:
    # Also mask configured/custom token environment variables, regardless of token format.
    secrets = sorted(
        {
            value
            for key, value in os.environ.items()
            if _SECRET_NAME.search(key) and len(value) >= 4
        },
        key=len,
        reverse=True,
    )
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    value = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value)
    value = _USERINFO.sub(r"\1[REDACTED]@", value)
    value = _QUERY.sub(r"\1?[REDACTED]", value)
    value = _AUTHORIZATION.sub(r"\1[REDACTED]", value)
    value = _ASSIGNMENT.sub(r"\1[REDACTED]", value)
    return redact_source(value.encode("utf-8", errors="replace")).content.decode("utf-8")


def debug_event(logger: logging.Logger, event: str, **fields: object) -> None:
    if logger.isEnabledFor(logging.DEBUG):
        # Redact before JSON escaping; escape newlines and terminal controls in output.
        safe = {key: _redact(str(value)) for key, value in fields.items()}
        logger.debug("%s %s", event, json.dumps(safe, ensure_ascii=True))


class GitTrace:
    """Correlate a command with bounded, redacted stderr, including on cancellation."""

    def __init__(self, logger: logging.Logger, argv: Sequence[str], env: Mapping[str, str]) -> None:
        self.logger = logger
        self.enabled = logger.isEnabledFor(logging.DEBUG)
        self.command_id = uuid4().hex
        self.started = time.monotonic()
        self.stderr = bytearray()
        self.stderr_bytes = 0
        if self.enabled:
            debug_event(logger, "Git command started", command_id=self.command_id, argv=list(argv))
            # Only the explicitly constructed Git environment is eligible, never os.environ.
            debug_event(logger, "Git environment", command_id=self.command_id, **env)
            omitted = sorted(
                key
                for key in os.environ
                if key not in env
                and (
                    key.startswith("GIT_")
                    or key.lower().endswith("_proxy")
                    or key in {"GH_TOKEN", "GITHUB_TOKEN", "SSL_CERT_FILE", "SSL_CERT_DIR"}
                )
            )
            debug_event(
                logger, "Git omitted environment names", command_id=self.command_id, names=omitted
            )

    def capture(self, chunk: bytes) -> None:
        self.stderr_bytes += len(chunk)
        if self.enabled:
            self.stderr.extend(chunk[: max(0, 65536 - len(self.stderr))])

    def finish(self, status: int | None, outcome: str) -> None:
        if self.enabled:
            debug_event(
                self.logger,
                "Git command finished",
                command_id=self.command_id,
                status=status,
                outcome=outcome,
                elapsed_s=round(time.monotonic() - self.started, 3),
                stderr_bytes=self.stderr_bytes,
                stderr_truncated=self.stderr_bytes > len(self.stderr),
                stderr=self.stderr.decode("utf-8", errors="replace"),
            )
