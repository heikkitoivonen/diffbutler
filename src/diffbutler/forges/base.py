"""Read-only forge seam. Review execution and publishing are separate capabilities."""

from typing import Protocol

from diffbutler.domain.forge import (
    Account,
    Discovery,
    ForgeHealth,
    PRPage,
    PRQuery,
    PRRef,
    PullRequest,
)


class ForgeError(Exception):
    """Safe diagnostic, never an HTTP body, URL with credentials, or auth output."""

    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class ReadForge(Protocol):
    @property
    def id(self) -> str: ...
    async def whoami(self) -> Account: ...
    async def list_review_requests(self) -> Discovery: ...
    async def search_pull_requests(self, query: PRQuery, cursor: str | None = None) -> PRPage: ...
    async def get_pull_request(self, ref: PRRef) -> PullRequest: ...
    async def health(self) -> ForgeHealth: ...
    async def close(self) -> None: ...
