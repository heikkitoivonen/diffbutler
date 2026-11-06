"""Offline number lookup through paginated repository lists, without PR search."""

import asyncio

import httpx
import pytest
from tests.test_github import API, adapter, pull

from diffbutler.core.pr_lookup import numbered_prs
from diffbutler.domain.forge import ReadBudget
from diffbutler.forges.base import ForgeError
from diffbutler.forges.github.adapter import GitHubForge
from diffbutler.forges.github.http import GitHubHTTP


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["User", "Organization"])
async def test_number_lookup_avoids_incomplete_search_and_keeps_all_matches(kind: str) -> None:
    lookups: list[str] = []
    pages: list[int] = []
    endpoint = "/users/acme/repos" if kind == "User" else "/orgs/acme/repos"

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/search/issues":
            pytest.fail("Number lookup must not use the incomplete owner-wide PR search")
        if path == "/users/acme":
            return httpx.Response(200, json={"type": kind})
        if path == endpoint:
            page = int(request.url.params["page"])
            pages.append(page)
            names = ("api", "closed", "merged", "issue") if page == 1 else ("api", "web")
            return httpx.Response(
                200,
                json=[{"full_name": f"Acme/{name}"} for name in names],
                headers={"Link": f'<{API}{endpoint}?page=2>; rel="next"'} if page == 1 else {},
            )
        if path == "/user/repos":
            assert request.url.params["visibility"] == "private"
            assert request.url.params["affiliation"] == "owner,collaborator"
            return httpx.Response(200, json=[{"full_name": "Acme/private"}])
        lookups.append(path)
        assert path.endswith("/pulls/1")
        repo = path.split("/")[3]
        if repo == "issue":
            return httpx.Response(404)
        data = pull(
            state="closed" if repo in {"closed", "merged"} else "open", merged=repo == "merged"
        )
        data["base"]["repo"]["full_name"] = f"Acme/{repo}"
        return httpx.Response(200, json=data)

    forge = adapter(handle)
    forge.http.request_interval = 0
    try:
        results = await numbered_prs(forge, ["acme/api", "acme/*"], 1)
        assert {pr.ref.repository for pr in results} == (
            {"api", "web", "private"} if kind == "User" else {"api", "web"}
        )
        assert pages == [1, 2]
        assert lookups.count("/repos/acme/api/pulls/1") == 1
    finally:
        await forge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [False, True])
async def test_personal_owner_includes_only_owned_public_and_accessible_private_repositories(
    authenticated: bool,
) -> None:
    private_pages: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/users/acme":
            return httpx.Response(200, json={"type": "User"})
        if path == "/users/acme/repos":
            assert request.url.params["type"] == "owner"
            return httpx.Response(200, json=[{"full_name": "Acme/public"}])
        assert authenticated and path == "/user/repos"
        assert request.url.params["visibility"] == "private"
        assert request.url.params["affiliation"] == "owner,collaborator"
        page = int(request.url.params["page"])
        private_pages.append(page)
        return httpx.Response(
            200,
            json=[{"full_name": "Other/private" if page == 1 else "ACME/private"}],
            headers={"Link": f'<{API}/user/repos?page=2>; rel="next"'} if page == 1 else {},
        )

    forge = GitHubForge(
        GitHubHTTP(
            API,
            "synthetic" if authenticated else None,
            transport=httpx.MockTransport(handle),
            request_interval=0,
        )
    )
    try:
        assert await forge.list_repositories("ACME") == (
            ("acme/private", "acme/public") if authenticated else ("acme/public",)
        )
        assert private_pages == ([1, 2] if authenticated else [])
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_repository_listing_is_not_capped_at_one_thousand_results() -> None:
    pages: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/acme":
            return httpx.Response(200, json={"type": "Organization"})
        assert request.url.path == "/orgs/acme/repos"
        page = int(request.url.params["page"])
        pages.append(page)
        start = (page - 1) * 100
        return httpx.Response(
            200,
            json=[{"full_name": f"acme/repo{n}"} for n in range(start, min(start + 100, 1001))],
            headers={"Link": f'<{API}/orgs/acme/repos?page={page + 1}>; rel="next"'}
            if page < 11
            else {},
        )

    forge = adapter(handle)
    forge.http.request_interval = 0
    try:
        assert len(await forge.list_repositories("acme")) == 1001
        assert pages == list(range(1, 12))
    finally:
        await forge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sso", "forbidden", "repeated", "invalid", "malformed"])
async def test_repository_listing_failure_never_returns_partial_matches(failure: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/acme":
            return httpx.Response(200, json={"type": "Organization"})
        assert request.url.path == "/orgs/acme/repos"
        page = int(request.url.params["page"])
        headers = {"Link": f'<{API}/orgs/acme/repos?page={page + 1}>; rel="next"'}
        if page == 1 or failure == "repeated":
            return httpx.Response(200, json=[{"full_name": "acme/api"}], headers=headers)
        if failure == "forbidden":
            return httpx.Response(403)
        if failure == "sso":
            return httpx.Response(200, json=[], headers={"x-github-sso": "partial-results"})
        return httpx.Response(200, json=[{"full_name": "../bad"}] if failure == "invalid" else {})

    forge = adapter(handle)
    forge.http.request_interval = 0
    try:
        with pytest.raises(ForgeError):
            await numbered_prs(forge, ["acme/*"], 1)
    finally:
        await forge.close()


def open_pull(path: str) -> httpx.Response:
    data = pull()
    data["base"]["repo"]["full_name"] = "/".join(path.split("/")[2:4])
    return httpx.Response(200, json=data)


@pytest.mark.asyncio
async def test_repositories_that_cannot_hold_open_prs_are_not_read() -> None:
    lookups: list[str] = []
    listed = [
        {"full_name": "acme/quiet", "open_issues_count": 0},
        {"full_name": "acme/disabled", "open_issues_count": 3, "disabled": True},
        {"full_name": "acme/busy", "open_issues_count": 2},
        {"full_name": "acme/uncounted"},
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/acme":
            return httpx.Response(200, json={"type": "Organization"})
        if request.url.path == "/orgs/acme/repos":
            return httpx.Response(200, json=listed)
        lookups.append(request.url.path)
        return open_pull(request.url.path)

    forge = adapter(handle)
    forge.http.request_interval = 0
    try:
        assert await forge.list_repositories("acme") == ("acme/busy", "acme/uncounted")
        results = await numbered_prs(forge, ["acme/*", "acme/quiet"], 1)
        assert {pr.ref.repository for pr in results} == {"busy", "uncounted", "quiet"}
        assert sorted(lookups) == [
            f"/repos/acme/{name}/pulls/1" for name in ("busy", "quiet", "uncounted")
        ]
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_missing_owner_contributes_no_matches() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/gone":
            return httpx.Response(404)
        assert request.url.path == "/repos/acme/api/pulls/1"
        return open_pull(request.url.path)

    forge = adapter(handle)
    forge.http.request_interval = 0
    try:
        (result,) = await numbered_prs(forge, ["acme/api", "gone/*"], 1)
        assert result.ref.repository == "api"
    finally:
        await forge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["User", "Organization"])
async def test_owner_listing_with_another_owner_reports_the_configured_owner(kind: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/acme":
            return httpx.Response(200, json={"type": kind})
        assert request.url.path in {"/orgs/acme/repos", "/users/acme/repos"}
        # A renamed owner lists its repositories under the new name.
        return httpx.Response(200, json=[{"full_name": "acme-co/api", "open_issues_count": 0}])

    forge = adapter(handle)
    forge.http.request_interval = 0
    try:
        with pytest.raises(ForgeError, match="outside owner acme; if it was renamed"):
            await forge.list_repositories("acme")
    finally:
        await forge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("listed", "allowed"), [(33, True), (34, False)])
async def test_anonymous_lookup_refuses_reads_that_would_drain_core_quota(
    listed: int, allowed: bool
) -> None:
    lookups: list[str] = []
    responses = 0

    def quota() -> dict[str, str]:
        # Each response reports one request fewer remaining, starting from 50.
        nonlocal responses
        responses += 1
        return {
            "x-ratelimit-resource": "core",
            "x-ratelimit-limit": "60",
            "x-ratelimit-remaining": str(51 - responses),
            "x-ratelimit-reset": "1003600",
        }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/acme":
            return httpx.Response(200, json={"type": "User"}, headers=quota())
        if request.url.path == "/users/acme/repos":
            names = [{"full_name": f"acme/r{n}", "open_issues_count": 1} for n in range(listed)]
            return httpx.Response(200, json=names, headers=quota())
        lookups.append(request.url.path)
        response = open_pull(request.url.path)
        response.headers.update(quota())
        return response

    forge = GitHubForge(
        GitHubHTTP(
            API,
            None,
            transport=httpx.MockTransport(handle),
            now=lambda: 1_000_000.0,
            request_interval=0,
        )
    )
    try:
        # After listing, 49 remain; the 6-request low-quota reserve leaves 43 spare,
        # and 10 of those stay reserved for following the chosen PR.
        if allowed:
            assert len(await numbered_prs(forge, ["acme/*"], 1)) == listed
            assert len(lookups) == listed
            # The scan ends with exactly the follow-up reserve above the pacing threshold.
            assert forge.read_budget().requests == 10
        else:
            with pytest.raises(ForgeError, match=r"34 more repositories .* can spare \(43\)"):
                await numbered_prs(forge, ["acme/*"], 1)
            assert not lookups
            assert forge.read_budget().requests == 43
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_read_budget_ignores_expired_quota_and_uses_the_request_interval() -> None:
    http = GitHubHTTP(API, None, now=lambda: 2_000_000.0, request_interval=0.5)
    try:
        assert http.read_budget() == ReadBudget(None, 0.5)
        http.record_limit(
            httpx.Headers(
                {
                    "x-ratelimit-resource": "core",
                    "x-ratelimit-limit": "60",
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": "1999999",
                }
            )
        )
        assert http.read_budget().requests is None
    finally:
        await http.close()


class SlowTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"login": "reader"})


@pytest.mark.asyncio
async def test_read_budget_uses_observed_latency_above_the_request_interval() -> None:
    http = GitHubHTTP(API, "synthetic", transport=SlowTransport(), request_interval=0.01)
    try:
        await http.get("user")
        assert http.read_budget().seconds_per_request >= 0.05
    finally:
        await http.close()
