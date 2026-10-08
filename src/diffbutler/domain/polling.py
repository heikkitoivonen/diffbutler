"""Pure quota budgeting for read-only polling; timestamps are supplied by the caller."""

from dataclasses import dataclass

from diffbutler.domain.forge import RateLimit


@dataclass(frozen=True)
class PollQuota:
    host: str
    login: str
    quota: RateLimit
    requests: int
    observed_at: float

    @property
    def key(self) -> tuple[str, str, str]:
        return self.host, self.login.casefold(), self.quota.resource


class QuotaBudget:
    """One account/resource's conservative cost estimate and threshold hysteresis."""

    def __init__(self, minimum: float) -> None:
        self.minimum = minimum
        self.interval = minimum
        self.sample: PollQuota | None = None
        self.average_cost = 0.0
        self.cost = 0.0
        self.external_rate = 0.0
        self.low = False
        self.high_samples = 0
        self.wait_for_reset = False

    def update(self, sample: PollQuota, *, successful: bool, now: float) -> None:
        previous = self.sample
        quota = sample.quota
        reset = quota.reset_at.timestamp()
        if previous is not None and previous.quota.reset_at != quota.reset_at:
            # Keep workload estimates, but never carry old-window depletion forward.
            self.external_rate = 0
            self.interval = self.minimum
            self.low = False
            self.high_samples = 0
        elif previous is not None and sample.observed_at > previous.observed_at:
            elapsed = sample.observed_at - previous.observed_at
            external = max(0, previous.quota.remaining - quota.remaining - sample.requests)
            self.external_rate = 0.3 * external / elapsed + 0.7 * self.external_rate
        self.sample = sample
        if not self.average_cost:
            self.average_cost = max(1, sample.requests)
        elif successful:
            self.average_cost = 0.3 * sample.requests + 0.7 * self.average_cost
        else:
            # An incomplete refresh is not evidence that the queue became cheaper.
            self.average_cost = max(self.average_cost, sample.requests)
        estimated_cost = 1.25 * max(1, sample.requests, self.average_cost)
        self.cost = estimated_cost if successful else max(self.cost, estimated_cost)

        fraction = quota.remaining / quota.limit
        if fraction < 0.2:
            if not self.low:
                self.interval *= 1.5
            self.high_samples = 0
        elif fraction > 0.8 and successful:
            self.high_samples += 1
            if self.high_samples >= 2:
                self.interval *= 0.8
                self.high_samples = 0
        else:
            self.high_samples = 0
        self.low = fraction < 0.2

        remaining_time = max(0, reset - now)
        available = (
            quota.remaining
            - 0.1 * quota.limit
            - self.external_rate * max(0, reset - sample.observed_at)
        )
        self.wait_for_reset = available < self.cost and remaining_time > 0
        budget_interval = self.cost * remaining_time / available if available > 0 else 0
        self.interval = max(self.minimum, self.interval, budget_interval)

    def next_start(self, started: float, now: float) -> float:
        assert self.sample is not None
        reset = self.sample.quota.reset_at.timestamp() + 1
        if now >= reset:
            # Wake to get fresh headers, even if the previous interval was very long.
            return started + self.minimum
        if self.wait_for_reset:
            return reset
        return min(started + self.interval, reset)
