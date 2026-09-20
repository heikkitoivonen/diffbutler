"""Bounded async GET transport with conditional reads and origin confinement."""

import asyncio
import math
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import ClassVar

import httpx

from diffbutler.domain.forge import RateLimit
from diffbutler.forges.base import ForgeError


def api_url(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ForgeError("GitHub base_url must be a valid HTTPS API URL") from None
    if (
        url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or url.path not in {"", "/", "/api/v3", "/api/v3/"}
    ):
        raise ForgeError(
            "GitHub base_url must be HTTPS, with no credentials/query and an optional /api/v3 path"
        )
    return url.copy_with(path=url.path.rstrip("/") + "/")


@dataclass(frozen=True)
class ReadResponse:
    body: object
    headers: httpx.Headers
    next_page: bool


class GitHubHTTP:
    DEFAULT_REQUEST_INTERVAL: ClassVar[float] = 1.0

    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.time,
        request_interval: float | None = None,
    ) -> None:
        self.base_url = api_url(base_url)
        if token is not None and (
            not token or not token.isascii() or any(character.isspace() for character in token)
        ):
            raise ForgeError("GitHub token is empty or contains whitespace")
        self.authenticated = token is not None
        self.request_interval = (
            self.DEFAULT_REQUEST_INTERVAL if request_interval is None else request_interval
        )
        if not math.isfinite(self.request_interval) or self.request_interval < 0:
            raise ValueError("Request interval must be finite and nonnegative")
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            transport=transport,
            timeout=20,
            follow_redirects=False,
            headers={
                **({"Authorization": f"Bearer {token}"} if token is not None else {}),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "DiffButler",
            },
        )
        self.sleep = sleep
        self.now = now
        self.last_request: float | None = None
        self.last_resource_request: dict[str, float] = {}
        self.limits: dict[str, RateLimit] = {}
        self.cache: OrderedDict[str, ReadResponse] = OrderedDict()
        self.blocked_until = 0.0
        self.lock = asyncio.Lock()

    async def paced_get(self, url: httpx.URL, headers: dict[str, str]) -> httpx.Response:
        """Pace every wire request, including redirects and retry attempts."""
        resource = "search" if url.path.endswith("/search/issues") else "core"
        limit = self.limits.get(resource)
        if limit and limit.remaining <= 0 and limit.reset_at.timestamp() > self.now():
            raise ForgeError(
                "GitHub rate limit is exhausted",
                retry_after=limit.reset_at.timestamp() - self.now(),
            )
        if self.blocked_until > self.now():
            raise ForgeError(
                "GitHub rate limit cooldown is active",
                retry_after=self.blocked_until - self.now(),
            )
        if self.request_interval:
            current = self.now()
            ready = current
            if self.last_request is not None:
                ready = max(ready, self.last_request + self.request_interval)
            previous = self.last_resource_request.get(resource)
            if previous is not None:
                interval = self.request_interval
                if resource == "search":
                    interval = max(interval, 2.1 if self.authenticated else 6.1)
                if limit and 0 < limit.remaining <= limit.limit * 0.2:
                    interval = max(
                        interval, (limit.reset_at.timestamp() - previous) / limit.remaining
                    )
                ready = max(ready, previous + interval)
            if ready > current:
                await self.sleep(ready - current)
        started = self.now()
        self.last_request = started
        self.last_resource_request[resource] = started
        response = await self.client.get(url, headers=headers)
        self.record_limit(response.headers)
        return response

    def checked_url(self, value: str | httpx.URL) -> httpx.URL:
        try:
            url = self.base_url.join(value)
        except httpx.InvalidURL:
            raise ForgeError("GitHub returned an invalid API URL") from None
        if (
            (url.scheme, url.host, url.port)
            != (self.base_url.scheme, self.base_url.host, self.base_url.port)
            or not url.path.startswith(self.base_url.path)
            or url.userinfo
            or url.fragment
        ):
            raise ForgeError("GitHub returned a URL outside the configured API origin")
        return url

    def record_limit(self, headers: httpx.Headers) -> None:
        try:
            resource = headers["x-ratelimit-resource"]
            self.limits[resource] = RateLimit(
                resource,
                int(headers["x-ratelimit-remaining"]),
                int(headers["x-ratelimit-limit"]),
                datetime.fromtimestamp(int(headers["x-ratelimit-reset"]), UTC),
            )
        except KeyError, ValueError, OverflowError, OSError:
            return

    def retry_delay(self, response: httpx.Response) -> float | None:
        headers = response.headers
        if response.status_code not in {403, 429}:
            return None
        if "retry-after" in headers:
            try:
                delay = float(headers["retry-after"])
                return max(0, delay) if math.isfinite(delay) else 60
            except ValueError:
                try:
                    return max(
                        0, parsedate_to_datetime(headers["retry-after"]).timestamp() - self.now()
                    )
                except ValueError, TypeError, OverflowError:
                    return 60
        if headers.get("x-ratelimit-remaining") == "0":
            try:
                delay = float(headers["x-ratelimit-reset"]) - self.now()
                return max(0, delay) if math.isfinite(delay) else 60
            except KeyError, ValueError:
                return 60
        # The response body is inspected only to classify the error, never surfaced.
        if response.status_code == 429 or "rate limit" in response.text.lower():
            return 60
        return None

    async def get(self, path: str, params: dict[str, str | int] | None = None) -> ReadResponse:
        url = self.checked_url(path)
        if params:
            url = url.copy_merge_params(params)
        async with self.lock:
            return await self.read(url)

    async def read(self, url: httpx.URL) -> ReadResponse:
        key = str(url)
        cached = self.cache.get(key)
        headers: dict[str, str] = {}
        if cached is not None:
            if "etag" in cached.headers:
                headers["If-None-Match"] = cached.headers["etag"]
            elif "last-modified" in cached.headers:
                headers["If-Modified-Since"] = cached.headers["last-modified"]
        redirects = 0
        for attempt in range(3):
            try:
                response = await self.paced_get(url, headers)
                while response.status_code in {301, 302, 303, 307, 308}:
                    redirects += 1
                    if redirects > 3 or "location" not in response.headers:
                        raise ForgeError("GitHub redirect limit exceeded or location missing")
                    url = self.checked_url(url.join(response.headers["location"]))
                    headers = {}
                    response = await self.paced_get(url, {})
            except httpx.RequestError:
                if attempt < 2:
                    await self.sleep(2**attempt + random.random())
                    continue
                raise ForgeError(
                    "GitHub request failed; check network, TLS, and API availability"
                ) from None
            delay = self.retry_delay(response)
            if delay is not None:
                delay = max(delay, 1)
                self.blocked_until = self.now() + delay
                # Return long waits to the caller instead of holding doctor open.
                if delay > 5 or attempt == 2:
                    raise ForgeError(
                        "GitHub rate limit reached; retry after the reported cooldown",
                        status=response.status_code,
                        retry_after=delay,
                    )
                await self.sleep(delay + random.random())
                continue
            if response.status_code >= 500 and attempt < 2:
                await self.sleep(2**attempt + random.random())
                continue
            if response.status_code == 304:
                self.blocked_until = 0
                if cached is None or redirects:
                    raise ForgeError("GitHub returned 304 without a cached representation")
                merged = httpx.Headers(cached.headers)
                merged.update(response.headers)
                result = ReadResponse(cached.body, merged, cached.next_page)
                self.cache[key] = result
                self.cache.move_to_end(key)
                return result
            if response.status_code != 200:
                messages = {
                    401: "GitHub authentication failed",
                    403: "GitHub access denied; check token permissions and organization SSO",
                    404: "GitHub resource is missing or inaccessible",
                    422: "GitHub rejected the search or request parameters",
                }
                raise ForgeError(
                    messages.get(response.status_code, "GitHub request failed"),
                    status=response.status_code,
                )
            try:
                body: object = response.json()
            except ValueError:
                raise ForgeError("GitHub returned invalid JSON") from None
            result = ReadResponse(body, response.headers, "next" in response.links)
            self.blocked_until = 0
            self.cache[key] = result
            self.cache.move_to_end(key)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
            return result
        raise ForgeError("GitHub retry budget exhausted")

    async def close(self) -> None:
        self.cache.clear()
        await self.client.aclose()
