"""Enumerate owned repositories for exact PR-number lookups without search limits."""

from typing import Literal

from diffbutler.domain.forge import identity_part, repository_parts
from diffbutler.forges.base import ForgeError
from diffbutler.forges.github.http import GitHubHTTP
from diffbutler.forges.github.mapping import Repository, ResponseModel, objects, parse


class Owner(ResponseModel):
    type: Literal["User", "Organization"]


async def repository_names(
    http: GitHubHTTP, endpoint: str, params: dict[str, str | int]
) -> set[str]:
    names: set[str] = set()
    page = 1
    while True:
        response = await http.get(endpoint, {**params, "per_page": 100, "page": page})
        if response.headers.get("x-github-sso"):
            raise ForgeError(
                "Organization SSO restricts repository lookup; "
                "authorize SSO or use the full PR link"
            )
        current: set[str] = set()
        for item in objects(response.body):
            name = parse(Repository, item).full_name
            try:
                repository_parts(name)
            except ValueError:
                raise ForgeError("GitHub returned an invalid repository identity") from None
            current.add(name.lower())
        if response.next_page and not current.difference(names):
            raise ForgeError(
                "Repository pagination made no progress; retry or use the full PR link"
            )
        names.update(current)
        if not response.next_page:
            return names
        page += 1


async def list_repositories(http: GitHubHTTP, owner: str) -> tuple[str, ...]:
    """Include public repositories and token-visible private repositories for the owner."""
    try:
        owner = identity_part(owner).lower()
    except ValueError:
        raise ForgeError("Invalid repository owner") from None
    response = await http.get(f"users/{owner}")
    kind = parse(Owner, response.body).type
    if kind == "Organization":
        names = await repository_names(http, f"orgs/{owner}/repos", {"type": "all"})
    else:
        names = await repository_names(http, f"users/{owner}/repos", {"type": "owner"})
        if http.authenticated:
            # The public user endpoint excludes private repos, even with a token.
            # Include private repos owned by this user that the token can access,
            # including those shared with the authenticated user as a collaborator.
            names.update(await repository_names(http, "user/repos", {"visibility": "private"}))
    return tuple(sorted(name for name in names if name.split("/", 1)[0] == owner))
