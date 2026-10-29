"""Offline number lookup through paginated repository lists, without PR search."""

import httpx
import pytest
from tests.test_github import API, adapter, pull

from diffbutler.core.pr_lookup import numbered_prs
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
