"""Read-only resolution of explicit review targets across repository scopes."""

from dataclasses import dataclass

from diffbutler.domain.forge import Account, PRRef, PullRequest, repository_matches
from diffbutler.forges.base import ForgeError, ReadForge


@dataclass(frozen=True)
class ReviewTarget:
    forge_index: int
    account: Account
    pull_request: PullRequest


async def open_pr(forge: ReadForge, ref: PRRef) -> PullRequest | None:
    try:
        pr = await forge.get_pull_request(ref)
    except ForgeError as exc:
        if exc.status == 404:
            return None
        raise
    if pr.ref != ref:
        raise ForgeError("Forge returned a different PR identity")
    return pr if pr.state == "open" else None


async def numbered_prs(
    forge: ReadForge, repositories: list[str], number: int
) -> tuple[PullRequest, ...]:
    """Resolve exact numbers directly after expanding owner repository scopes."""
    found: dict[PRRef, PullRequest] = {}
    checked: set[PRRef] = set()

    async def check(ref: PRRef) -> None:
        if ref not in checked:
            checked.add(ref)
            if pr := await open_pr(forge, ref):
                found[ref] = pr

    for repository in dict.fromkeys(repositories):
        if not repository.endswith("/*"):
            await check(forge.ref(f"{repository}#{number}"))
            continue
        for name in await forge.list_repositories(repository.removesuffix("/*")):
            ref = forge.ref(f"{name}#{number}")
            if ref.forge != forge.id or not repository_matches(repository, ref):
                raise ForgeError("Forge returned a repository outside the requested owner")
            await check(ref)
    return tuple(found.values())
