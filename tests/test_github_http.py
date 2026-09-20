"""HTTP failure handling and token confinement using an offline transport."""

import httpx
import pytest

from diffbutler.forges.base import ForgeError
from diffbutler.forges.github.http import GitHubHTTP, api_url


@pytest.mark.asyncio
async def test_conditional_get_reuses_body_and_pagination_then_observes_updates() -> None:
    count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count == 1:
            assert "if-none-match" not in request.headers
            return httpx.Response(
                200,
                json={"value": "old"},
                headers={
                    "ETag": '"v1"',
                    "Link": '<https://api.github.com/user?page=2>; rel="next"',
                },
            )
        assert request.headers["if-none-match"] == '"v1"'
        if count == 2:
            return httpx.Response(
                304,
                headers={
                    "X-RateLimit-Resource": "core",
                    "X-RateLimit-Remaining": "49",
                    "X-RateLimit-Limit": "50",
                    "X-RateLimit-Reset": "2000000000",
                },
            )
        return httpx.Response(200, json={"value": "new"}, headers={"ETag": '"v2"'})

    http = GitHubHTTP(
        "https://api.github.com", "synthetic-token", transport=httpx.MockTransport(handle)
    )
    try:
        first = await http.get("user")
        second = await http.get("user")
        assert second.body == first.body and second.next_page
        assert http.limits["core"].remaining == 49
        third = await http.get("user")
        assert third.body == {"value": "new"} and not third.next_page
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_last_modified_and_unexpected_304() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/missing":
            return httpx.Response(304)
        if "if-modified-since" in request.headers:
            assert request.headers["if-modified-since"] == "Tue, 15 Sep 2026 00:00:00 GMT"
            return httpx.Response(304)
        return httpx.Response(
            200, json=[], headers={"Last-Modified": "Tue, 15 Sep 2026 00:00:00 GMT"}
        )

    http = GitHubHTTP(
        "https://api.github.com", "synthetic-token", transport=httpx.MockTransport(handle)
    )
    try:
        await http.get("user")
        assert (await http.get("user")).body == []
        with pytest.raises(ForgeError, match="without a cached"):
            await http.get("missing")
    finally:
        await http.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://evil.invalid/steal",
        "http://api.github.com/user",
        "https://user:password@api.github.com/user",
        "https://git.example.invalid/outside",
    ],
)
async def test_redirects_cannot_send_token_outside_api_origin(location: str) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["authorization"] == "Bearer synthetic-token"
        return httpx.Response(302, headers={"Location": location})

    http = GitHubHTTP(
        "https://git.example.invalid/api/v3",
        "synthetic-token",
        transport=httpx.MockTransport(handle),
    )
    try:
        with pytest.raises(ForgeError, match="outside"):
            await http.get("user")
        assert calls == 1
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_same_origin_redirect_and_read_only_method() -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        paths.append(request.url.path)
        return (
            httpx.Response(301, headers={"Location": "/repos/new/api/pulls/1"})
            if len(paths) == 1
            else httpx.Response(200, json={"ok": True})
        )

    http = GitHubHTTP(
        "https://api.github.com", "synthetic-token", transport=httpx.MockTransport(handle)
    )
    try:
        assert (await http.get("repos/old/api/pulls/1")).body == {"ok": True}
        assert paths == ["/repos/old/api/pulls/1", "/repos/new/api/pulls/1"]
    finally:
        await http.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "headers", "delay"),
    [
        (429, {"Retry-After": "120"}, 120),
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"}, 100),
        (429, {}, 60),
    ],
)
async def test_long_rate_limit_returns_cooldown_without_early_retry(
    code: int, headers: dict[str, str], delay: int
) -> None:
    calls = 0

    async def sleep(_: float) -> None:
        pytest.fail("long cooldown must be returned to the caller")

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(code, headers=headers, text="PRIVATE TOKEN BODY")

    http = GitHubHTTP(
        "https://api.github.com",
        "synthetic-token",
        transport=httpx.MockTransport(handle),
        sleep=sleep,
        now=lambda: 1000,
    )
    try:
        for _ in range(2):
            with pytest.raises(ForgeError) as error:
                await http.get("user")
            assert error.value.retry_after == delay
            assert "PRIVATE" not in str(error.value)
        assert calls == 1
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_short_retry_after_is_honored() -> None:
    delays: list[float] = []
    current = 1000.0

    async def sleep(value: float) -> None:
        nonlocal current
        delays.append(value)
        current += value

    def handle(_: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, json={})
            if delays
            else httpx.Response(429, headers={"Retry-After": "2"})
        )

    http = GitHubHTTP(
        "https://api.github.com",
        "synthetic-token",
        transport=httpx.MockTransport(handle),
        sleep=sleep,
        now=lambda: current,
    )
    try:
        await http.get("user")
        assert len(delays) == 1 and 2 <= delays[0] < 3
        await http.get("user")
    finally:
        await http.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "server"])
async def test_transient_retries_are_bounded(failure: str) -> None:
    delays: list[float] = []
    calls = 0

    async def sleep(value: float) -> None:
        delays.append(value)

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "network":
            raise httpx.ConnectError("PRIVATE URL AND TOKEN")
        return httpx.Response(503, text="PRIVATE RESPONSE")

    http = GitHubHTTP(
        "https://api.github.com",
        "synthetic-token",
        transport=httpx.MockTransport(handle),
        sleep=sleep,
    )
    try:
        with pytest.raises(ForgeError) as error:
            await http.get("user")
        assert calls == 3 and len(delays) == 2
        assert 1 <= delays[0] < 2 and 2 <= delays[1] < 3
        assert "PRIVATE" not in str(error.value)
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_success_with_exhausted_quota_prevents_more_core_requests() -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={},
            headers={
                "X-RateLimit-Resource": "core",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Limit": "50",
                "X-RateLimit-Reset": "1100",
            },
        )

    http = GitHubHTTP(
        "https://api.github.com",
        "synthetic-token",
        transport=httpx.MockTransport(handle),
        now=lambda: 1000,
    )
    try:
        await http.get("user")
        with pytest.raises(ForgeError) as error:
            await http.get("user/teams")
        assert error.value.retry_after == 100 and calls == 1
    finally:
        await http.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com",
        "https://name:SECRET@api.github.com",
        "https://api.github.com?secret=yes",
        "https://git.example.invalid/random/path",
        "https://git.example.invalid:invalid",
    ],
)
def test_invalid_api_configuration_is_safe(url: str) -> None:
    with pytest.raises(ForgeError) as error:
        api_url(url)
    assert "SECRET" not in str(error.value)
