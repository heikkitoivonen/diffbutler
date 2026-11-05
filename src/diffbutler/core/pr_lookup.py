"""Read-only resolution of explicit review targets across repository scopes."""

import asyncio
import math
from dataclasses import dataclass

from diffbutler.domain.forge import Account, PRRef, PullRequest, repository_matches
from diffbutler.forges.base import ForgeError, ReadForge


@dataclass(frozen=True)
class ReviewTarget:
    forge_index: int
    account: Account
    pull_request: PullRequest


async def open_pr(forge: ReadForge, ref: PRRef, *, listed: bool = False) -> PullRequest | None:
    """Open PR at ref, or None when it is missing or, for listed repositories, unreadable."""
    try:
        pr = await forge.get_pull_request(ref)
    except ForgeError as exc:
        # A listed repository can deny PR reads (disabled, blocked, or metadata-only
        # access); rate limits carry retry_after and still fail the lookup.
        if exc.status == 404 or (listed and exc.status in {403, 451} and exc.retry_after is None):
            return None
        raise
    if pr.ref != ref:
        raise ForgeError("Forge returned a different PR identity")
    return pr if pr.state == "open" else None


# Reads kept in reserve after a scan: the account check, then following the chosen
# PR (account checks, the PR, and its first activity pages) on a fresh client.
FOLLOW_UP_READS = 10


def check_budget(forge: ReadForge, number: int, reads: int, deadline: float | None) -> None:
    """Fail before reads that would reach low-quota pacing or outlast the deadline."""
    budget = forge.read_budget()
    if budget.requests is not None and reads + FOLLOW_UP_READS > budget.requests:
        raise ForgeError(
            f"Checking PR #{number} in {reads} more repositories needs more requests "
            f"than the forge's rate limit can spare ({budget.requests}); "
            "enter the full PR link or retry after the limit resets"
        )
    if deadline is not None:
        needed = (reads + FOLLOW_UP_READS) * budget.seconds_per_request
        if needed > deadline - asyncio.get_running_loop().time():
            raise ForgeError(
                f"Checking PR #{number} in {reads} more repositories and following it "
                f"would take about {math.ceil(needed)} seconds, longer than the lookup "
                "allows; enter the full PR link"
            )


async def numbered_prs(
    forge: ReadForge, repositories: list[str], number: int, *, deadline: float | None = None
) -> tuple[PullRequest, ...]:
    """Resolve exact numbers directly after expanding owner repository scopes.

    deadline is an event-loop time. When owner scopes were listed, each read first
    checks that the remaining reads fit the forge's spare quota and the deadline.
    """
    refs: dict[PRRef, bool] = {}
    for repository in dict.fromkeys(repositories):
        if not repository.endswith("/*"):
            refs[forge.ref(f"{repository}#{number}")] = False
            continue
        for name in await forge.list_repositories(repository.removesuffix("/*")):
            ref = forge.ref(f"{name}#{number}")
            if ref.forge != forge.id or not repository_matches(repository, ref):
                raise ForgeError("Forge returned a repository outside the requested owner")
            refs.setdefault(ref, True)
    budgeted = any(refs.values())
    found: list[PullRequest] = []
    for index, (ref, listed) in enumerate(refs.items()):
        if budgeted:
            # Rechecking before every read also covers retries that used extra quota.
            check_budget(forge, number, len(refs) - index, deadline)
        if pr := await open_pr(forge, ref, listed=listed):
            found.append(pr)
    return tuple(found)
