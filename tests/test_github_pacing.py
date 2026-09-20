"""Real pacing logic with a virtual clock, never real network calls or waits."""

import asyncio

import httpx
import pytest

from diffbutler.forges.base import ForgeError
from diffbutler.forges.github.http import GitHubHTTP


class Clock:
    def __init__(self) -> None:
        self.time = 1000.0
        self.delays: list[float] = []

    def now(self) -> float:
        return self.time

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.time += delay


@pytest.mark.asyncio
@pytest.mark.parametrize("token,search_gap", [(None, 6.1), ("synthetic", 2.1)])
async def test_paces_core_and_search_requests(token: str | None, search_gap: float) -> None:
    clock = Clock()
    starts: list[float] = []

    def handle(_: httpx.Request) -> httpx.Response:
        starts.append(clock.now())
        return httpx.Response(200, json={})

    http = GitHubHTTP(
        "https://api.github.com",
        token,
        transport=httpx.MockTransport(handle),
        sleep=clock.sleep,
        now=clock.now,
        request_interval=1,
    )
    try:
        await http.get("user")
        await http.get("search/issues")
        await http.get("user")
        await http.get("search/issues")
        assert starts == pytest.approx([1000, 1001, 1002, 1001 + search_gap])
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_low_quota_spreads_remaining_requests_until_reset() -> None:
    clock = Clock()
    starts: list[float] = []

    def handle(_: httpx.Request) -> httpx.Response:
        starts.append(clock.now())
        return httpx.Response(
            200,
            json={},
            headers={
                "X-RateLimit-Resource": "core",
                "X-RateLimit-Limit": "10",
                "X-RateLimit-Remaining": str(3 - len(starts)),
                "X-RateLimit-Reset": "1100",
            },
        )

    http = GitHubHTTP(
        "https://api.github.com",
        None,
        transport=httpx.MockTransport(handle),
        sleep=clock.sleep,
        now=clock.now,
        request_interval=1,
    )
    try:
        await http.get("users/reader")
        await http.get("repos/acme/api/pulls/1")
        await http.get("repos/acme/api/pulls/2")
        assert starts == [1000, 1050, 1100]
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_concurrent_calls_share_the_request_spacing() -> None:
    clock = Clock()
    starts: list[float] = []

    def handle(_: httpx.Request) -> httpx.Response:
        starts.append(clock.now())
        return httpx.Response(200, json={})

    http = GitHubHTTP(
        "https://api.github.com",
        None,
        transport=httpx.MockTransport(handle),
        sleep=clock.sleep,
        now=clock.now,
        request_interval=1,
    )
    try:
        await asyncio.gather(*(http.get(f"repos/acme/api/pulls/{number}") for number in range(3)))
        assert starts == [1000, 1001, 1002]
        clock.time += 10
        await http.get("users/reader")
        assert starts[-1] == 1012  # Time spent idle already satisfies the gap.
    finally:
        await http.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 503])
async def test_redirects_and_retries_respect_exhausted_quota(status: int) -> None:
    clock = Clock()
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={
                "Location": "/repos/new/api/pulls/1",
                "X-RateLimit-Resource": "core",
                "X-RateLimit-Limit": "60",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1100",
            },
        )

    http = GitHubHTTP(
        "https://api.github.com",
        None,
        transport=httpx.MockTransport(handle),
        sleep=clock.sleep,
        now=clock.now,
        request_interval=1,
    )
    try:
        with pytest.raises(ForgeError, match="exhausted"):
            await http.get("repos/old/api/pulls/1")
        assert calls == 1
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_redirects_and_retries_are_paced() -> None:
    clock = Clock()
    starts: list[float] = []

    def handle(_: httpx.Request) -> httpx.Response:
        starts.append(clock.now())
        if len(starts) == 1:
            return httpx.Response(301, headers={"Location": "/repos/new/api/pulls/1"})
        if len(starts) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={})

    http = GitHubHTTP(
        "https://api.github.com",
        "synthetic",
        transport=httpx.MockTransport(handle),
        sleep=clock.sleep,
        now=clock.now,
        request_interval=3,
    )
    try:
        await http.get("repos/old/api/pulls/1")
        assert starts == [1000, 1003, 1006]
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_cancelling_a_paced_request_prevents_sending_it() -> None:
    clock = Clock()
    waiting = asyncio.Event()
    calls = 0

    async def sleep(_: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    http = GitHubHTTP(
        "https://api.github.com",
        None,
        transport=httpx.MockTransport(handle),
        sleep=sleep,
        now=clock.now,
        request_interval=1,
    )
    try:
        await http.get("users/reader")
        task = asyncio.create_task(http.get("repos/acme/api/pulls/1"))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == 1 and not http.lock.locked()
    finally:
        await http.close()
