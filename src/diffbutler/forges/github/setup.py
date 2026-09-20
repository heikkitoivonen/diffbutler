"""Read-only visibility and credential checks before first-run configuration is saved."""

from diffbutler.domain.forge import PRQuery, identity_part
from diffbutler.forges.base import ForgeError
from diffbutler.forges.github.http import GitHubHTTP
from diffbutler.forges.github.mapping import ResponseModel, User, parse


class RepositoryAccess(ResponseModel):
    full_name: str
    private: bool
    visibility: str | None = None


async def repository_access(http: GitHubHTTP, repository: str) -> RepositoryAccess:
    PRQuery(repository=repository)
    response = await http.get(f"repos/{repository}")
    access = parse(RepositoryAccess, response.body)
    if access.full_name.lower() != repository.lower():
        raise ForgeError("GitHub returned a different repository; check owner/name")
    return access


async def repository_is_public(base_url: str, repository: str) -> bool:
    http = GitHubHTTP(base_url, None)
    try:
        access = await repository_access(http, repository)
        return not access.private and access.visibility in {None, "public"}
    except ForgeError as exc:
        if exc.status in {401, 403, 404} and exc.retry_after is None:
            return False
        raise
    finally:
        await http.close()


async def validate_access(
    base_url: str,
    repository: str,
    login: str,
    token: str | None,
) -> str:
    identity_part(login)
    http = GitHubHTTP(base_url, token)
    try:
        try:
            response = await http.get("user" if token is not None else f"users/{login}")
        except ForgeError as exc:
            if exc.status == 401:
                raise ForgeError(
                    "Invalid GitHub token in the selected environment variable"
                ) from None
            if exc.status == 404 and token is None:
                raise ForgeError("GitHub username was not found") from None
            raise
        user = parse(User, response.body)
        try:
            identity_part(user.login)
        except ValueError:
            raise ForgeError("GitHub returned an invalid account identity") from None
        if user.login.lower() != login.lower():
            raise ForgeError("GitHub token belongs to a different username")
        await repository_access(http, repository)
        # Validate pull-request read permissions, not just repository metadata access.
        await http.get(f"repos/{repository}/pulls", {"per_page": 1, "state": "open"})
        return user.login
    finally:
        await http.close()
