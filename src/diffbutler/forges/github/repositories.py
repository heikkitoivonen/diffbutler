"""Enumerate owned repositories for exact PR-number lookups without search limits."""

from typing import Literal

from pydantic import Field

from diffbutler.domain.forge import identity_part, repository_parts
from diffbutler.forges.base import ForgeError
from diffbutler.forges.github.http import GitHubHTTP
from diffbutler.forges.github.mapping import ResponseModel, objects, parse


class Owner(ResponseModel):
    type: Literal["User", "Organization"]


class ListedRepository(ResponseModel):
    full_name: str
    # GitHub counts open pull requests as issues; absent counts keep the repository.
    open_issues_count: int | None = Field(default=None, ge=0)
    disabled: bool = False


async def repository_names(
    http: GitHubHTTP,
    endpoint: str,
    params: dict[str, str | int],
    owner: str,
    *,
    other_owners: bool = False,
) -> set[str]:
    """Owner repositories that can hold an open PR; other owners skip or fail."""
    names: set[str] = set()
    seen: set[str] = set()
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
            repository = parse(ListedRepository, item)
            try:
                repository_parts(repository.full_name)
            except ValueError:
                raise ForgeError("GitHub returned an invalid repository identity") from None
            name = repository.full_name.lower()
            current.add(name)
            if name.split("/", 1)[0] != owner:
                if other_owners:
                    continue
                raise ForgeError(
                    f"GitHub listed repositories outside owner {owner}; "
                    "if it was renamed, update the repositories setting"
                )
            if not repository.disabled and repository.open_issues_count != 0:
                names.add(name)
        if response.next_page and not current.difference(seen):
            raise ForgeError(
                "Repository pagination made no progress; retry or use the full PR link"
            )
        seen.update(current)
        if not response.next_page:
            return names
        page += 1


async def list_repositories(http: GitHubHTTP, owner: str) -> tuple[str, ...]:
    """Owner repositories, public and token-visible private, that can hold an open PR."""
    try:
        owner = identity_part(owner).lower()
    except ValueError:
        raise ForgeError("Invalid repository owner") from None
    try:
        response = await http.get(f"users/{owner}")
    except ForgeError as exc:
        if exc.status == 404:
            # Like a missing exact repository, a missing owner has no accessible PRs.
            return ()
        raise
    kind = parse(Owner, response.body).type
    if kind == "Organization":
        return tuple(
            sorted(await repository_names(http, f"orgs/{owner}/repos", {"type": "all"}, owner))
        )
    names = await repository_names(http, f"users/{owner}/repos", {"type": "owner"}, owner)
    if http.authenticated:
        # The public user endpoint excludes private repos, even with a token.
        # Include private repos owned by this user that the token can access,
        # including those shared with the authenticated user as a collaborator.
        # Organization membership is excluded: those repos have organization owners.
        names.update(
            await repository_names(
                http,
                "user/repos",
                {"visibility": "private", "affiliation": "owner,collaborator"},
                owner,
                other_owners=True,
            )
        )
    return tuple(sorted(names))
