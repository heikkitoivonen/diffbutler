"""Synthetic GitHub REST fixtures: no network, credentials, or model calls."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from diffbutler.domain.forge import PRQuery
from diffbutler.forges.base import ForgeError, ReadForge
from diffbutler.forges.github.adapter import GitHubForge
from diffbutler.forges.github.http import GitHubHTTP

API = "https://api.github.com"


def pull(
    number: int = 1,
    *,
    users: tuple[str, ...] = ("reader",),
    teams: tuple[str, ...] = (),
    state: str = "open",
    merged: bool = False,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Synthetic change {number}",
        "user": {"login": "author"},
        "state": state,
        "merged": merged,
        "draft": False,
        "head": {"sha": "a" * 40, "ref": "topic", "repo": None},
        "base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": "Acme/API"}},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-15T00:00:00Z",
        "requested_reviewers": [{"login": user} for user in users],
        "requested_teams": [{"slug": team} for team in teams],
    }


def search(*numbers: int, total: int | None = None, incomplete: bool = False) -> dict[str, Any]:
    return {
        "total_count": len(numbers) if total is None else total,
        "incomplete_results": incomplete,
        "items": [
            {"pull_request": {"url": f"{API}/repos/acme/api/pulls/{number}"}} for number in numbers
        ],
    }


def adapter(
    handler: Callable[[httpx.Request], httpx.Response], *, base: str = API, teams: bool = True
) -> GitHubForge:
    return GitHubForge(
        GitHubHTTP(base, "synthetic-token", transport=httpx.MockTransport(handler)),
        include_team_requests=teams,
    )


@pytest.mark.asyncio
async def test_direct_and_team_discovery_deduplicates_and_keeps_provenance() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "Reader", "name": None})
        if request.url.path == "/user/teams":
            return httpx.Response(
                200,
                headers={"X-OAuth-Scopes": "repo, read:org"},
                json=[{"slug": "Reviewers", "organization": {"login": "Acme"}}],
            )
        if request.url.path == "/search/issues":
            return httpx.Response(
                200,
                json=search(1, 2)
                if "team-review-requested:" in request.url.params["q"]
                else search(1),
            )
        number = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(
            200, json=pull(number, users=("READER",) if number == 1 else (), teams=("reviewers",))
        )

    forge = adapter(handle)
    contract: ReadForge = forge
    try:
        result = await contract.list_review_requests()
        assert result.complete and not result.warnings
        assert len(result.items) == 2
        assert result.items[0].sources == ("team:acme/reviewers", "user:reader")
        assert result.items[1].sources == ("team:acme/reviewers",)
        assert sum(request.url.path == "/repos/acme/api/pulls/1" for request in calls) == 1
        assert {request.method for request in calls} == {"GET"}
    finally:
        await contract.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [403, 404, 401])
async def test_team_access_failure_preserves_direct_requests(failure: int) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "reader"})
        if request.url.path == "/user/teams":
            return httpx.Response(failure, json={"message": "PRIVATE RESPONSE"})
        return httpx.Response(
            200, json=search(1) if request.url.path == "/search/issues" else pull()
        )

    forge = adapter(handle)
    try:
        result = await forge.list_review_requests()
        assert not result.complete and len(result.items) == 1
        assert result.items[0].sources == ("user:reader",)
        assert "PRIVATE RESPONSE" not in str(result.warnings)
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_team_pagination_and_scoped_token_visibility() -> None:
    pages: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        pages.append(page)
        return httpx.Response(
            200,
            json=[{"slug": f"team-{page}", "organization": {"login": "acme"}}],
            headers={"Link": f'<{API}/user/teams?page=2>; rel="next"'} if page == "1" else {},
        )

    forge = adapter(handle)
    try:
        result = await forge.team_membership()
        assert pages == ["1", "2"]
        assert result.teams == ("acme/team-1", "acme/team-2")
        assert not result.complete
        assert "token-scoped" in result.warnings[0]
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_stale_search_hits_are_not_current_requests_and_team_opt_out() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path != "/user/teams"
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "reader"})
        if request.url.path == "/search/issues":
            return httpx.Response(200, json=search(1, 2))
        return httpx.Response(
            200,
            json=pull(users=(), teams=("reviewers",))
            if request.url.path.endswith("/1")
            else pull(2, state="closed"),
        )

    forge = adapter(handle, teams=False)
    try:
        result = await forge.list_review_requests()
        assert result.complete and not result.items
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_search_filters_pagination_and_direct_lookup() -> None:
    queries: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            queries.append(request.url.params["q"])
            page = int(request.url.params["page"])
            return httpx.Response(
                200,
                json=search(page, total=2),
                headers={"Link": f'<{API}/search/issues?page=2>; rel="next"'} if page == 1 else {},
            )
        return httpx.Response(
            200,
            json=pull(
                int(request.url.path.rsplit("/", 1)[1]), state="closed", merged=True, users=()
            ),
        )

    forge = adapter(handle)
    try:
        query = PRQuery(
            repository="acme/api", author="author", state="merged", text="fix bug", page_size=1
        )
        first = await forge.search_pull_requests(query)
        second = await forge.search_pull_requests(query, first.next_cursor)
        assert first.next_cursor == "2" and second.next_cursor is None
        assert first.complete and second.complete
        assert [item.ref.number for item in (*first.items, *second.items)] == [1, 2]
        assert queries == ['is:pr is:merged repo:acme/api author:author "fix bug"'] * 2
        direct = await forge.get_pull_request(
            forge.ref("https://github.com/ACME/API/pull/2#discussion")
        )
        assert direct.state == "merged" and direct.ref == forge.ref("acme/api#2")
        assert direct.created_at.tzinfo is not None and direct.head_sha == "a" * 40
    finally:
        await forge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("total", "incomplete"), [(1001, False), (1, True)])
async def test_search_limitations_are_explicit(total: int, incomplete: bool) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=search(1, total=total, incomplete=incomplete)
            if request.url.path == "/search/issues"
            else pull(),
        )

    forge = adapter(handle)
    try:
        result = await forge.search_pull_requests(PRQuery())
        assert not result.complete and result.warnings and len(result.items) == 1
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_one_inaccessible_result_does_not_discard_accessible_prs() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            return httpx.Response(200, json=search(1, 2))
        return (
            httpx.Response(404)
            if request.url.path.endswith("/2")
            else httpx.Response(200, json=pull())
        )

    forge = adapter(handle)
    try:
        result = await forge.search_pull_requests(PRQuery())
        assert not result.complete and len(result.items) == 1
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_enterprise_base_path_and_unrelated_instance_rejected() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://git.example.invalid/api/v3/repos/acme/api/pulls/1"
        return httpx.Response(200, json=pull())

    forge = adapter(handle, base="https://git.example.invalid/api/v3")
    try:
        ref = forge.ref("https://git.example.invalid/acme/api/pull/1")
        result = await forge.get_pull_request(ref)
        assert result.url == "https://git.example.invalid/Acme/API/pull/1"
        assert ref.forge == "github:https://git.example.invalid/api/v3"
        with pytest.raises(ForgeError, match="different"):
            forge.ref("https://github.com/acme/api/pull/1")
    finally:
        await forge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"number": "1"},
        {**pull(), "requested_reviewers": "invalid"},
        {**pull(), "updated_at": "SECRET"},
    ],
)
async def test_malformed_responses_do_not_leak_private_values(payload: dict[str, Any]) -> None:
    forge = adapter(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(ForgeError) as error:
            await forge.get_pull_request(forge.ref("acme/api#1"))
        assert "SECRET" not in str(error.value)
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_invalid_references_and_cursors_do_not_send_requests() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        pytest.fail("invalid inputs must not make HTTP requests")

    forge = adapter(handle)
    try:
        for value in (
            "../api#1",
            "acme/api#0",
            "https://evil.invalid/acme/api/pull/1",
            "acme/api#1; touch file",
        ):
            with pytest.raises(ForgeError):
                forge.ref(value)
        for cursor in ("0", "-1", "https://evil.invalid", "1001"):
            with pytest.raises(ForgeError):
                await forge.search_pull_requests(PRQuery(), cursor)
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_direct_discovery_failure_preserves_team_results() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "reader"})
        if request.url.path == "/user/teams":
            return httpx.Response(
                200,
                headers={"X-OAuth-Scopes": "read:org"},
                json=[{"slug": "reviewers", "organization": {"login": "acme"}}],
            )
        if request.url.path == "/search/issues":
            if "team-review-requested:" in request.url.params["q"]:
                return httpx.Response(200, json=search(1))
            return httpx.Response(403)
        return httpx.Response(200, json=pull(users=(), teams=("reviewers",)))

    forge = adapter(handle)
    try:
        result = await forge.list_review_requests()
        assert not result.complete and len(result.items) == 1
        assert result.items[0].sources == ("team:acme/reviewers",)
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_later_discovery_rechecks_membership_even_when_search_is_unchanged() -> None:
    refreshed = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal refreshed
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "reader"})
        if request.url.path == "/search/issues":
            if "if-none-match" in request.headers:
                return httpx.Response(304)
            return httpx.Response(200, json=search(1), headers={"ETag": '"same-search"'})
        refreshed += 1
        data = pull(users=("reader",) if refreshed == 1 else ())
        data["head"]["sha"] = "new-sha" if refreshed > 1 else "old-sha"
        return httpx.Response(200, json=data)

    forge = adapter(handle, teams=False)
    try:
        first = await forge.list_review_requests()
        second = await forge.list_review_requests()
        assert first.complete and len(first.items) == 1
        assert second.complete and not second.items and refreshed == 2
        # Direct identity refresh remains available after request membership disappears.
        assert (await forge.get_pull_request(first.items[0].pull_request.ref)).head_sha == "new-sha"
    finally:
        await forge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.invalid/repos/acme/api/pulls/1",
        "https://api.github.com:invalid/repos/acme/api/pulls/1",
    ],
)
async def test_untrusted_search_urls_are_not_requested(url: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/issues"
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [{"pull_request": {"url": url}}],
            },
        )

    forge = adapter(handle)
    try:
        result = await forge.search_pull_requests(PRQuery())
        assert not result.complete and not result.items
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_activity_paginates_requests_and_reviews_and_filters_teams() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        path = request.url.path
        calls.append(path)
        if path == "/user":
            return httpx.Response(200, json={"login": "reader"})
        if path == "/user/teams":
            return httpx.Response(
                200,
                headers={"x-oauth-scopes": "read:org"},
                json=[
                    {"slug": "reviewers", "organization": {"login": "acme"}},
                ],
            )
        if path.endswith("/comments"):
            return httpx.Response(200, json=[])
        page = int(request.url.params["page"])
        headers = {"link": f'<{API}{path}?page=2>; rel="next"'} if page == 1 else {}
        if path.endswith("/events"):
            return httpx.Response(
                200,
                headers=headers,
                json=[
                    {
                        "id": page,
                        "event": "review_requested",
                        "created_at": "2026-09-01T00:00:00Z",
                        **(
                            {"requested_reviewer": {"login": "reader"}}
                            if page == 1
                            else {"requested_team": {"slug": "reviewers"}}
                        ),
                    },
                    {
                        "id": 100,
                        "event": "review_requested",
                        "created_at": "2026-09-01T00:00:00Z",
                        "requested_reviewer": {"login": "someone-else"},
                    },
                ],
            )
        return httpx.Response(
            200,
            headers=headers,
            json=[
                {
                    "id": page,
                    "user": {"login": "reader"},
                    "state": "APPROVED",
                    "submitted_at": "2026-09-02T00:00:00Z",
                    "commit_id": "head",
                },
                {
                    "id": 100,
                    "user": {"login": "reader"},
                    "state": "PENDING",
                    "submitted_at": None,
                    "commit_id": None,
                },
            ],
        )

    forge = adapter(handle)
    try:
        activity = await forge.get_review_activity(forge.ref("acme/api#1"))
        assert activity.complete and not activity.warnings
        assert len(activity.requests) == len(activity.reviews) == 2
        assert activity.requests[0].id != activity.requests[1].id
        assert calls.count("/repos/acme/api/issues/1/events") == 2
        assert calls.count("/repos/acme/api/pulls/1/reviews") == 2
    finally:
        await forge.close()


@pytest.mark.asyncio
async def test_partial_activity_preserves_results_and_safe_warning() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "reader"})
        if request.url.path.endswith("/events"):
            return httpx.Response(403, json={"message": "PRIVATE BODY"})
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "user": {"login": "reader"},
                    "state": "APPROVED",
                    "submitted_at": "2026-09-02T00:00:00Z",
                    "commit_id": "head",
                },
            ],
        )

    forge = adapter(handle, teams=False)
    try:
        activity = await forge.get_review_activity(forge.ref("acme/api#1"))
        assert not activity.complete and len(activity.reviews) == 1
        assert activity.warnings and "PRIVATE BODY" not in str(activity)
    finally:
        await forge.close()
