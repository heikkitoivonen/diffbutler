"""Task-scoped rate-limit observations for sync diagnostics."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace

from diffbutler.domain.forge import RateLimit
from diffbutler.domain.polling import PollQuota


@dataclass(eq=False)
class RateLimitSource:
    """Client identity, resolved by whoami; never contains credentials."""

    host: str
    authenticated: bool
    login: str | None = None


@dataclass
class ResourceObservations:
    requests: int = 0
    latest: RateLimit | None = None
    observed_at: float = 0


@dataclass
class RateLimitObservations:
    minimum_core: RateLimit | None = None
    resources: dict[tuple[RateLimitSource, str], ResourceObservations] = field(
        default_factory=dict[tuple[RateLimitSource, str], ResourceObservations]
    )
    cooldown_until: float = 0

    def quotas(self) -> tuple[PollQuota, ...]:
        latest: dict[tuple[str, str, str], PollQuota] = {}
        requests: dict[tuple[str, str, str], int] = {}
        for (source, resource), observed in self.resources.items():
            # Anonymous clients on one host share its per-IP quota.
            login = source.login if source.authenticated else ""
            if login is None:
                continue
            key = source.host, login.casefold(), resource
            requests[key] = requests.get(key, 0) + observed.requests
            if observed.latest is None:
                continue
            sample = PollQuota(
                source.host, login, observed.latest, observed.requests, observed.observed_at
            )
            previous = latest.get(key)
            if previous is None or sample.observed_at >= previous.observed_at:
                latest[key] = sample
        return tuple(replace(sample, requests=requests[key]) for key, sample in latest.items())


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


def observe_request(source: RateLimitSource, resource: str) -> None:
    observations = _observations.get()
    if observations is not None:
        observed = observations.resources.setdefault((source, resource), ResourceObservations())
        observed.requests += 1


def observe_cooldown(until: float) -> None:
    observations = _observations.get()
    if observations is not None:
        observations.cooldown_until = max(observations.cooldown_until, until)


def observe_rate_limit(
    limit: RateLimit, source: RateLimitSource | None = None, *, observed_at: float = 0
) -> None:
    observations = _observations.get()
    if observations is not None and source is not None:
        observed = observations.resources.setdefault(
            (source, limit.resource), ResourceObservations()
        )
        observed.latest = limit
        observed.observed_at = observed_at
    if (
        observations is not None
        and limit.resource == "core"
        and (
            observations.minimum_core is None
            or limit.remaining < observations.minimum_core.remaining
        )
    ):
        observations.minimum_core = limit
