"""Task-scoped rate-limit observations for sync diagnostics."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from diffbutler.domain.forge import RateLimit


@dataclass
class RateLimitObservations:
    minimum: RateLimit | None = None


_observations: ContextVar[RateLimitObservations | None] = ContextVar(
    "forge_rate_limit_observations", default=None
)


@contextmanager
def collect_rate_limits() -> Generator[RateLimitObservations]:
    observations = RateLimitObservations()
    token = _observations.set(observations)
    try:
        yield observations
    finally:
        _observations.reset(token)


def observe_rate_limit(limit: RateLimit) -> None:
    observations = _observations.get()
    if observations is not None and (
        observations.minimum is None or limit.remaining < observations.minimum.remaining
    ):
        observations.minimum = limit
