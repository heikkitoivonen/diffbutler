"""Read-only resolution of explicit review targets across repository scopes."""

from dataclasses import dataclass

from diffbutler.domain.forge import Account, PRQuery, PRRef, PullRequest, repository_matches
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
    """Exact repositories use direct reads; wildcard scopes require complete pagination."""
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
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = await forge.search_pull_requests(
                PRQuery(repository=repository, page_size=100, number=number), cursor
            )
            if not page.complete:
                raise ForgeError("PR lookup is incomplete; retry before choosing a repository")
            for pr in page.items:
                if (
                    pr.ref.forge == forge.id
                    and pr.ref.number == number
                    and pr.state == "open"
                    and repository_matches(repository, pr.ref)
                ):
                    await check(pr.ref)
            cursor = page.next_cursor
            if cursor is None:
                break
            if cursor in seen:
                raise ForgeError("Repeated pagination cursor during PR lookup")
            seen.add(cursor)
    return tuple(found.values())
