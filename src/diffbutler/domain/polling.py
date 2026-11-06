"""Pure quota budgeting for read-only polling; timestamps are supplied by the caller."""

from dataclasses import dataclass

from diffbutler.domain.forge import RateLimit

# Share of each quota left unspent by polling for interactive actions and other clients.
RESERVE_SHARE = 0.1


@dataclass(frozen=True)
class PollQuota:
    host: str
    login: str  # Empty for anonymous access, which no GitHub login can collide with.
    quota: RateLimit
    requests: int
    observed_at: float

    @property
    def key(self) -> tuple[str, str, str]:
        return self.host, self.login.casefold(), self.quota.resource


class QuotaBudget:
    """Spreads one account/resource's spare quota evenly over the time until reset.

    Each update recomputes the interval from the latest headers, so consumption by
    other clients is reflected in the next refresh's remaining count.
    """

    def __init__(self, minimum: float) -> None:
        self.minimum = minimum
        self.interval = minimum
        self.sample: PollQuota | None = None
        self.average_cost = 0.0
        self.cost = 0.0
        self.wait_for_reset = False

    def update(self, sample: PollQuota, *, successful: bool, now: float) -> None:
        self.sample = sample
        requests = max(1, sample.requests)
        if not self.average_cost:
            self.average_cost = requests
        elif successful:
            self.average_cost = 0.5 * requests + 0.5 * self.average_cost
        else:
            # An incomplete refresh is not evidence that the queue became cheaper.
            self.average_cost = max(self.average_cost, requests)
        cost = max(requests, self.average_cost)
        self.cost = cost if successful else max(self.cost, cost)

        quota = sample.quota
        remaining_time = max(0, quota.reset_at.timestamp() - now)
        usable = quota.remaining - RESERVE_SHARE * quota.limit
        self.wait_for_reset = usable < self.cost and remaining_time > 0
        budget_interval = self.cost * remaining_time / usable if usable > 0 else 0
        self.interval = max(self.minimum, budget_interval)

    def next_start(self, started: float, now: float) -> float:
        assert self.sample is not None
        reset = self.sample.quota.reset_at.timestamp() + 1
        if now >= reset:
            # Wake to get fresh headers, even if the previous interval was very long.
            return started + self.minimum
        if self.wait_for_reset:
            return reset
        return min(started + self.interval, reset)
