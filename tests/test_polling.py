"""Adaptive polling uses synthetic response headers and an explicit clock."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from diffbutler.core.polling import Poller
from diffbutler.domain.forge import RateLimit
from diffbutler.domain.polling import PollQuota, QuotaBudget
from diffbutler.forges.base import ForgeError
from diffbutler.forges.diagnostics import (
    RateLimitSource,
    collect_rate_limits,
    observe_cooldown,
    observe_rate_limit,
    observe_request,
)
from diffbutler.forges.github.adapter import GitHubForge
from diffbutler.forges.github.http import GitHubHTTP


def sample(
    remaining: int = 900,
    *,
    limit: int = 5000,
    now: float = 1000,
    reset: float = 4000,
    requests: int = 80,
    resource: str = "core",
    login: str = "reader",
) -> PollQuota:
    return PollQuota(
        "https://api.github.com",
        login,
        RateLimit(resource, remaining, limit, datetime.fromtimestamp(reset, UTC)),
        requests,
        now,
    )


def observe(value: PollQuota, *, authenticated: bool = True) -> None:
    source = RateLimitSource(value.host, authenticated, value.login)
    for _ in range(value.requests):
        observe_request(source, value.quota.resource)
    observe_rate_limit(value.quota, source, observed_at=value.observed_at)


@pytest.mark.parametrize("reset,expected", [(4000, 750), (1120, 90)])
def test_budget_accounts_for_time_until_reset(reset: float, expected: float) -> None:
    budget = QuotaBudget(60)
    budget.update(sample(reset=reset), successful=True, now=1000)
    assert budget.cost == 100  # 25% above the measured 80 calls.
    assert budget.interval == expected
    assert budget.next_start(1000, 1000) == 1000 + expected


def test_large_queue_slows_even_above_eighty_percent() -> None:
    budget = QuotaBudget(60)
    budget.update(sample(4500, reset=4300), successful=True, now=1000)
    assert budget.interval == 82.5


def test_low_threshold_does_not_compound_and_high_threshold_needs_two_successes() -> None:
    budget = QuotaBudget(60)
    budget.update(sample(requests=1), successful=True, now=1000)
    assert budget.interval == 90
    budget.update(sample(requests=1, now=1060), successful=True, now=1060)
    assert budget.interval == 90
    budget.update(sample(3000, requests=1, now=1120), successful=True, now=1120)
    assert budget.interval == 90
    budget.update(sample(4500, requests=1, now=1180), successful=True, now=1180)
    assert budget.interval == 90
    budget.update(sample(4500, requests=1, now=1240), successful=False, now=1240)
    assert budget.interval == 90
    for now in (1300, 1360):
        budget.update(sample(4500, requests=1, now=now), successful=True, now=now)
    assert budget.interval == 72
    for now in (1420, 1480):
        budget.update(sample(4500, requests=1, now=now), successful=True, now=now)
    assert budget.interval == 60


@pytest.mark.parametrize("remaining", [0, 450, 550])
def test_insufficient_budget_waits_for_reset_then_reassesses(remaining: int) -> None:
    budget = QuotaBudget(60)
    budget.update(sample(remaining), successful=True, now=1000)
    assert budget.wait_for_reset
    assert budget.next_start(1000, 1500) == 4001
    assert budget.next_start(1000, 4001) <= 4001
    budget.update(sample(4999, now=4001, reset=7600), successful=True, now=4001)
    assert not budget.wait_for_reset
    assert budget.interval == pytest.approx(100 * 3599 / 4499)


def test_cost_surges_are_immediate_and_incomplete_refreshes_cannot_lower_estimate() -> None:
    budget = QuotaBudget(60)
    budget.update(sample(4000, requests=10), successful=True, now=1000)
    budget.update(sample(3500, requests=500, now=1060), successful=True, now=1060)
    assert budget.cost == 625
    before = budget.average_cost
    budget.update(sample(3499, requests=1, now=1120), successful=False, now=1120)
    assert budget.average_cost == before
    assert budget.cost == 625


def test_other_consumers_are_estimated_only_within_a_reset_window() -> None:
    budget = QuotaBudget(60)
    budget.update(sample(4000, requests=10), successful=True, now=1000)
    budget.update(sample(3000, requests=100, now=1100), successful=True, now=1100)
    assert budget.external_rate == pytest.approx(2.7)
    assert budget.wait_for_reset
    budget.update(sample(4900, now=4001, reset=7600), successful=True, now=4001)
    assert budget.external_rate == 0
    assert not budget.wait_for_reset


def test_collection_groups_clients_by_account_and_keeps_latest_window() -> None:
    with collect_rate_limits() as observations:
        observe(sample(900))
        observe(sample(4999, login="READER", now=4001, reset=7600))
        observe(sample(100, login="other"))
        observe(replace(sample(100), host="https://enterprise.example/api/v3"))
        observe(sample(10, limit=30, resource="search", reset=1060))
        observe(sample(), authenticated=False)
    quotas = {quota.key: quota for quota in observations.quotas()}
    assert len(quotas) == 4
    combined = quotas[("https://api.github.com", "reader", "core")]
    assert combined.quota.remaining == 4999
    assert combined.quota.reset_at.timestamp() == 7600
    assert combined.requests == 160
    with collect_rate_limits() as fresh:
        assert fresh.quotas() == ()


@pytest.mark.asyncio
async def test_poll_deadline_combines_accounts_resources_duration_and_cooldowns() -> None:
    now = 1000.0

    async def refresh() -> None:
        nonlocal now
        observe(sample())  # Needs 750 seconds between starts.
        observe(sample(4999, login="other"))
        observe(sample(0, limit=30, resource="search", reset=1060, requests=1))
        now += 30

    poller = Poller(refresh, 60, now=lambda: now)
    await poller.refresh_observed(refresh)
    # Budget is computed at completion, using the remaining reset time.
    assert poller.delay() == pytest.approx(100 * 2970 / 400 - 30)
    poller.cooldown_until = 2000
    assert poller.delay() == 970
    with pytest.raises(ForgeError, match="cooldown"):
        await poller.refresh_observed(refresh)


@pytest.mark.asyncio
async def test_failed_sync_retains_quota_and_swallowed_server_cooldown() -> None:
    async def refresh() -> None:
        observe(sample(4500))
        observe_cooldown(5000)
        raise ForgeError("Incomplete refresh")

    poller = Poller(refresh, 60, now=lambda: 1000)
    with pytest.raises(ForgeError, match="Incomplete"):
        await poller.refresh_observed(refresh)
    assert poller.delay() == 4000
    assert poller.budgets and poller.failures == 1


@pytest.mark.asyncio
async def test_anonymous_and_missing_headers_keep_minimum_and_manual_refresh_reschedules() -> None:
    now = 1000.0

    async def refresh() -> None:
        observe(sample(0), authenticated=False)

    poller = Poller(refresh, 600, now=lambda: now)
    await poller.refresh_observed(refresh)
    assert not poller.budgets
    now += 100
    assert poller.delay() == 500
    poller.changed.clear()
    await poller.refresh_observed(refresh)
    assert poller.delay() == 600 and poller.changed.is_set()


@pytest.mark.asyncio
async def test_transport_counts_attempts_and_resolves_account_for_first_response() -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503 if calls == 1 else 200,
            json={"login": "Reader"},
            headers={
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": str(5000 - calls),
                "X-RateLimit-Reset": "4600",
                "X-RateLimit-Resource": "core",
            },
        )

    async def sleep(_: float) -> None:
        pass

    http = GitHubHTTP(
        "https://api.github.com",
        "synthetic",
        transport=httpx.MockTransport(handle),
        now=lambda: 1000,
        sleep=sleep,
    )
    forge = GitHubForge(http)
    try:
        with collect_rate_limits() as observations:
            await forge.whoami()
        (quota,) = observations.quotas()
        assert quota.login == "Reader" and quota.requests == 2
        assert quota.quota.remaining == 4998
    finally:
        await forge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,remaining,expected", [(429, 4000, 1600), (403, 0, 4601)])
async def test_transport_cooldown_survives_client_close_and_caught_error(
    status: int, remaining: int, expected: float
) -> None:
    http = GitHubHTTP(
        "https://api.github.com",
        "synthetic",
        now=lambda: 1000,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status,
                headers={
                    "Retry-After": "600",
                    "X-RateLimit-Limit": "5000",
                    "X-RateLimit-Remaining": str(remaining),
                    "X-RateLimit-Reset": "4600",
                    "X-RateLimit-Resource": "core",
                },
            )
        ),
    )

    async def refresh() -> None:
        try:
            await GitHubForge(http).whoami()
        except ForgeError:
            pass  # Sync may preserve partial results after a rate-limit failure.
        finally:
            await http.close()

    poller = Poller(refresh, 60, now=lambda: 1000)
    await poller.refresh_observed(refresh)
    assert poller.cooldown_until == expected
    assert poller.delay() == expected - 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,remaining", [(0, 0), (-1, 0), (5000, -1), (5000, 5001)])
async def test_invalid_quota_cannot_reach_polling_budget(limit: int, remaining: int) -> None:
    http = GitHubHTTP(
        "https://api.github.com",
        "synthetic",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"login": "reader"},
                headers={
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": str(remaining),
                    "X-RateLimit-Reset": "4600",
                    "X-RateLimit-Resource": "core",
                },
            )
        ),
    )
    try:
        with collect_rate_limits() as observations:
            await GitHubForge(http).whoami()
        assert http.limits == {} and observations.quotas() == ()
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_poller_wait_is_interruptible_after_manual_observation() -> None:
    calls = 0
    refreshed = asyncio.Event()

    async def refresh() -> None:
        nonlocal calls
        calls += 1
        refreshed.set()

    poller = Poller(refresh, 60)
    task = asyncio.create_task(poller.run())
    try:
        await asyncio.wait_for(refreshed.wait(), timeout=1)
        await poller.refresh_observed(refresh)
        assert calls == 2 and poller.delay() > 59
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
