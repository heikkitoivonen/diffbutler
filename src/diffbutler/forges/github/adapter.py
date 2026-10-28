"""Read-only GitHub adapter; search results never create tasks or start AI work."""

import json
import re

import httpx
from pydantic import ValidationError

from diffbutler.domain.forge import (
    Account,
    Discovery,
    ForgeHealth,
    PRPage,
    PRQuery,
    PRRef,
    PullRequest,
    RequestedPR,
    TeamMembership,
    identity_part,
    repository_matches,
    repository_parts,
)
from diffbutler.domain.sync import CommentEvent, RequestEvent, ReviewActivity, ReviewEvent
from diffbutler.forges.base import ForgeError
from diffbutler.forges.github.http import GitHubHTTP
from diffbutler.forges.github.mapping import (
    Comment,
    IssueEvent,
    Review,
    SearchResult,
    Team,
    User,
    objects,
    parse,
    pull_request,
    timestamp,
)


def repository_qualifier(repository: str) -> str:
    owner, name = repository_parts(repository, allow_wildcard=True)
    return f"user:{owner}" if name == "*" else f"repo:{repository}"


class GitHubForge:
    def __init__(
        self,
        http: GitHubHTTP,
        *,
        include_team_requests: bool = True,
        repositories: tuple[str, ...] = (),
        public_login: str | None = None,
    ) -> None:
        self.http = http
        self.include_team_requests = include_team_requests
        self.repositories = repositories
        self.public_login = public_login
        self.id = f"github:{str(http.base_url).rstrip('/')}"
        host = "github.com" if http.base_url.host == "api.github.com" else http.base_url.host
        self.web_base = str(http.base_url.copy_with(host=host, path="/")).rstrip("/")

    def ref(self, value: str) -> PRRef:
        """Accept owner/repo#number or a PR URL on this configured GitHub instance."""
        if value.startswith("https://"):
            try:
                url = httpx.URL(value)
                web = httpx.URL(self.web_base)
            except httpx.InvalidURL:
                raise ForgeError("Invalid GitHub PR URL") from None
            if (url.scheme, url.host, url.port) != (web.scheme, web.host, web.port) or url.userinfo:
                raise ForgeError("PR URL belongs to a different GitHub instance")
            match = re.fullmatch(r"/([^/]+)/([^/]+)/pull/([1-9][0-9]*)/?", url.path)
        else:
            match = re.fullmatch(r"([^/]+)/([^/#]+)#([1-9][0-9]*)", value)
        if match is None:
            raise ForgeError("PR reference must be owner/repo#number or a GitHub pull-request URL")
        try:
            return PRRef(self.id, match[1].lower(), match[2].lower(), int(match[3]))
        except ValueError:
            raise ForgeError("Invalid GitHub PR reference") from None

    async def whoami(self) -> Account:
        endpoint = "user"
        if not self.http.authenticated:
            if not self.public_login:
                raise ForgeError("Set forges.login to your GitHub username for public access")
            try:
                identity_part(self.public_login)
            except ValueError:
                raise ForgeError("Invalid public GitHub username") from None
            endpoint = f"users/{self.public_login}"
        response = await self.http.get(endpoint)
        user = parse(User, response.body)
        try:
            identity_part(user.login)
        except ValueError:
            raise ForgeError("GitHub returned an invalid account identity") from None
        self.http.observation_source.login = user.login
        return Account(self.id, user.login, user.name or user.login)

    async def team_membership(self) -> TeamMembership:
        if not self.http.authenticated:
            return TeamMembership(
                (),
                False,
                ("Team review requests require authentication; only direct requests are visible",),
            )
        teams: set[str] = set()
        warnings: list[str] = []
        for page in range(1, 101):
            try:
                response = await self.http.get("user/teams", {"per_page": 100, "page": page})
                scopes = {
                    scope.strip() for scope in response.headers.get("x-oauth-scopes", "").split(",")
                }
                if not scopes.intersection({"read:org", "user", "repo", "admin:org", "write:org"}):
                    warnings.append(
                        "Team visibility is token-scoped; "
                        "cross-organization coverage cannot be verified"
                    )
                if response.headers.get("x-github-sso"):
                    warnings.append("Organization SSO restricts team visibility")
                for item in objects(response.body):
                    team = parse(Team, item)
                    if team.organization is None:
                        raise ForgeError("GitHub team response lacks organization identity")
                    try:
                        org = identity_part(team.organization.login).lower()
                        slug = identity_part(team.slug).lower()
                    except ValueError:
                        raise ForgeError("GitHub returned invalid team identity") from None
                    teams.add(f"{org}/{slug}")
                if not response.next_page:
                    break
            except ForgeError as exc:
                warnings.append(f"Team membership is incomplete: {exc}")
                break
        else:
            warnings.append("Team pagination limit reached")
        return TeamMembership(tuple(sorted(teams)), not warnings, tuple(dict.fromkeys(warnings)))

    async def get_pull_request(self, ref: PRRef) -> PullRequest:
        if ref.forge != self.id:
            raise ForgeError("PR identity belongs to a different forge")
        response = await self.http.get(f"repos/{ref.namespace}/{ref.repository}/pulls/{ref.number}")
        result = pull_request(response.body, forge=self.id, web_base=self.web_base)
        if result.ref.number != ref.number:
            raise ForgeError("GitHub returned a different pull request number")
        return result

    def search_ref(self, value: str) -> PRRef:
        url = self.http.checked_url(value)
        path = url.path.removeprefix(self.http.base_url.path)
        match = re.fullmatch(r"repos/([^/]+)/([^/]+)/pulls/([1-9][0-9]*)", path)
        if match is None:
            raise ForgeError("GitHub search returned an invalid pull request URL")
        return self.ref(f"{match[1]}/{match[2]}#{match[3]}")

    async def search_page(
        self,
        query: str,
        page: int,
        page_size: int,
        refreshed: dict[PRRef, PullRequest] | None = None,
        *,
        number: int | None = None,
    ) -> PRPage:
        response = await self.http.get(
            "search/issues",
            {"q": query, "per_page": page_size, "page": page, "sort": "updated", "order": "desc"},
        )
        result = parse(SearchResult, response.body)
        warnings: list[str] = []
        if result.incomplete_results:
            warnings.append("GitHub search reported incomplete results")
        if result.total_count > 1000:
            warnings.append("GitHub search is limited to 1,000 results; narrow the filters")
        if response.headers.get("x-github-sso"):
            warnings.append("Organization SSO restricts search visibility")
        items: dict[PRRef, PullRequest] = {}
        for hit in result.items:
            try:
                url = hit.pull_request.get("url")
                if not url:
                    raise ForgeError("GitHub search result lacks a pull request URL")
                ref = self.search_ref(url)
                if number is not None and ref.number != number:
                    continue
                pr = refreshed.get(ref) if refreshed is not None else None
                if pr is None:
                    pr = await self.get_pull_request(ref)
                    if refreshed is not None:
                        refreshed[ref] = pr
                items[pr.ref] = pr
            except ForgeError as exc:
                warnings.append(f"A search result could not be refreshed: {exc}")
        has_next = response.next_page and page * page_size < min(result.total_count, 1000)
        return PRPage(
            tuple(items.values()),
            str(page + 1) if has_next else None,
            not warnings,
            tuple(dict.fromkeys(warnings)),
        )

    async def search_pull_requests(self, query: PRQuery, cursor: str | None = None) -> PRPage:
        try:
            page = int(cursor) if cursor is not None else 1
        except ValueError:
            raise ForgeError("Invalid search cursor") from None
        if page < 1 or (page - 1) * query.page_size >= 1000:
            raise ForgeError("Search cursor is outside GitHub's 1,000-result window")
        terms = ["is:pr"]
        if query.state != "all":
            terms.append(f"is:{query.state}")
        if query.repository:
            terms.append(repository_qualifier(query.repository))
        if query.author:
            terms.append(f"author:{query.author}")
        if query.text.strip():
            terms.append(json.dumps(query.text.strip(), ensure_ascii=False))
        return await self.search_page(" ".join(terms), page, query.page_size, number=query.number)

    async def list_review_requests(self) -> Discovery:
        account = await self.whoami()
        memberships = (
            await self.team_membership() if self.include_team_requests else TeamMembership((), True)
        )
        warnings = list(memberships.warnings)
        queries = [f"is:pr is:open review-requested:{account.login}"]
        queries.extend(f"is:pr is:open team-review-requested:{team}" for team in memberships.teams)
        if self.repositories:
            queries = [
                f"{query} {repository_qualifier(repo)}"
                for query in queries
                for repo in self.repositories
            ]
        discovered: dict[PRRef, RequestedPR] = {}
        refreshed: dict[PRRef, PullRequest] = {}
        # Hydration is shared between overlapping direct/team searches during one
        # discovery pass. Conditional GETs otherwise refresh remote state.
        for query in queries:
            page = 1
            while True:
                try:
                    result = await self.search_page(query, page, 100, refreshed)
                    warnings.extend(result.warnings)
                    for pr in result.items:
                        if pr.state != "open":
                            continue
                        if self.repositories and not any(
                            repository_matches(repo, pr.ref) for repo in self.repositories
                        ):
                            continue
                        sources: list[str] = []
                        if account.login.lower() in pr.requested_users:
                            sources.append(f"user:{account.login.lower()}")
                        sources.extend(
                            f"team:{team}"
                            for team in memberships.teams
                            if team in pr.requested_teams
                        )
                        if sources:
                            previous = discovered.get(pr.ref)
                            if previous is not None:
                                sources = sorted(set(previous.sources).union(sources))
                            discovered[pr.ref] = RequestedPR(pr, tuple(sources))
                    if result.next_cursor is None:
                        break
                    page = int(result.next_cursor)
                except ForgeError as exc:
                    warnings.append(f"Review-request discovery is incomplete: {exc}")
                    break
        return Discovery(tuple(discovered.values()), not warnings, tuple(dict.fromkeys(warnings)))

    async def get_review_activity(self, ref: PRRef) -> ReviewActivity:
        if ref.forge != self.id:
            raise ForgeError("PR identity belongs to a different forge")
        account = await self.whoami()
        memberships = (
            await self.team_membership() if self.include_team_requests else TeamMembership((), True)
        )
        warnings = list(memberships.warnings)
        requests: dict[str, RequestEvent] = {}
        reviews: dict[str, ReviewEvent] = {}
        comments: dict[str, CommentEvent] = {}
        prefix = f"repos/{ref.namespace}/{ref.repository}"
        endpoints = (
            f"{prefix}/issues/{ref.number}/events",
            f"{prefix}/pulls/{ref.number}/reviews",
            f"{prefix}/pulls/{ref.number}/comments",
            f"{prefix}/issues/{ref.number}/comments",
        )
        for endpoint in endpoints:
            for page in range(1, 101):
                try:
                    response = await self.http.get(endpoint, {"per_page": 100, "page": page})
                    if response.headers.get("x-github-sso"):
                        warnings.append("Organization SSO restricts review activity visibility")
                    for value in objects(response.body):
                        if endpoint.endswith("/events"):
                            event = parse(IssueEvent, value)
                            if event.event != "review_requested":
                                continue
                            user = event.requested_reviewer
                            team = event.requested_team
                            organization = (
                                team.organization.login.lower()
                                if team and team.organization
                                else ref.namespace
                            )
                            addressed = (
                                user is not None and user.login.lower() == account.login.lower()
                            ) or (
                                team is not None
                                and f"{organization}/{team.slug.lower()}" in memberships.teams
                            )
                            if addressed:
                                key = f"request:{event.id:020d}"
                                requests[key] = RequestEvent(key, timestamp(event.created_at))
                        elif endpoint.endswith("/comments"):
                            comment = parse(Comment, value)
                            kind = "inline" if "/pulls/" in endpoint else "discussion"
                            key = f"{kind}:{comment.id:020d}"
                            comments[key] = CommentEvent(
                                key,
                                timestamp(comment.created_at),
                                comment.user.login if comment.user else "[deleted]",
                                comment.body,
                                comment.commit_id or "",
                                f"review:{comment.pull_request_review_id:020d}"
                                if comment.pull_request_review_id is not None
                                else None,
                            )
                        else:
                            review = parse(Review, value)
                            if review.submitted_at is None or review.user is None:
                                continue
                            key = f"review:{review.id:020d}"
                            reviews[key] = ReviewEvent(
                                key,
                                timestamp(review.submitted_at),
                                review.user.login,
                                review.state,
                                review.commit_id or "",
                                review.body or "",
                            )
                    if not response.next_page:
                        break
                except ForgeError, ValidationError:
                    warnings.append("Review activity is incomplete; activity transitions deferred")
                    break
            else:
                warnings.append("Review activity pagination limit reached")
        return ReviewActivity(
            account,
            tuple(requests.values()),
            tuple(reviews.values()),
            not warnings,
            tuple(dict.fromkeys(warnings)),
            tuple(comments.values()),
        )

    async def health(self) -> ForgeHealth:
        account = await self.whoami()
        teams = await self.team_membership() if self.include_team_requests else None
        # /user uses the core quota. A separate search quota check is deferred
        # until discovery/search runs; these limits never imply full token access.
        return ForgeHealth(account, tuple(self.http.limits.values()), teams)

    async def close(self) -> None:
        await self.http.close()
