"""Serial polling with account-scoped quota budgets and server cooldowns."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from diffbutler.domain.polling import QuotaBudget
from diffbutler.forges.base import ForgeError
from diffbutler.forges.diagnostics import RateLimitObservations, collect_rate_limits


class Poller:
    """No reviewer capability; manual refreshes also update the next automatic deadline."""

    def __init__(
        self,
        refresh: Callable[[], Awaitable[None]],
        interval: float,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.refresh = refresh
        self.minimum = interval
        self.now = now
        self.paused = False
        self.budgets: dict[tuple[str, str, str], QuotaBudget] = {}
        self.cooldown_until = 0.0
        self.started = now()
        self.failures = 0
        self.generation = 0
        self.changed = asyncio.Event()

    @property
    def interval(self) -> float:
        return max([self.minimum, *(budget.interval for budget in self.budgets.values())])

    def delay(self) -> float:
        now = self.now()
        next_start = max(
            [
                self.started + self.minimum,
                *(budget.next_start(self.started, now) for budget in self.budgets.values()),
            ]
        )
        if self.failures:
            next_start = max(next_start, self.started + self.minimum * 2**self.failures)
        return max(0, next_start - now, self.cooldown_until - now)

    async def refresh_observed(
        self, operation: Callable[[], Awaitable[None]]
    ) -> RateLimitObservations:
        """Observe even failed/partial syncs, including errors the sync service catches."""
        if self.cooldown_until > self.now():
            raise ForgeError(
                "GitHub rate limit cooldown is active",
                retry_after=self.cooldown_until - self.now(),
            )
        self.started = self.now()
        self.generation += 1
        successful = False
        with collect_rate_limits() as observations:
            try:
                await operation()
                successful = True
                self.failures = 0
            except Exception:
                self.failures = min(self.failures + 1, 5)
                raise
            finally:
                self.cooldown_until = max(self.cooldown_until, observations.cooldown_until)
                for sample in observations.quotas():
                    if sample.quota.reset_at.timestamp() <= self.now():
                        continue
                    budget = self.budgets.setdefault(sample.key, QuotaBudget(self.minimum))
                    previous = (budget.interval, budget.wait_for_reset)
                    budget.update(sample, successful=successful, now=self.now())
                    if previous != (budget.interval, budget.wait_for_reset):
                        logging.getLogger(__name__).info(
                            "Polling adjusted host=%s login=%s resource=%s interval_s=%.1f "
                            "remaining=%d limit=%d reset_in_s=%.1f cost=%.1f "
                            "external_calls_per_s=%.3f wait_for_reset=%s",
                            sample.host,
                            sample.login,
                            sample.quota.resource,
                            budget.interval,
                            sample.quota.remaining,
                            sample.quota.limit,
                            sample.quota.reset_at.timestamp() - self.now(),
                            budget.cost,
                            budget.external_rate,
                            budget.wait_for_reset,
                        )
                self.changed.set()
        return observations

    async def run(self) -> None:
        while True:
            if not self.paused:
                generation = self.generation
                started = self.now()
                try:
                    await self.refresh()
                    self.failures = 0
                except Exception:
                    if generation == self.generation:
                        self.failures = min(self.failures + 1, 5)
                finally:
                    # Also support refresh callbacks that don't collect quota headers.
                    if generation == self.generation:
                        self.started = started
            while True:
                self.changed.clear()
                delay = self.minimum if self.paused else self.delay()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=delay)
                except TimeoutError:
                    break
