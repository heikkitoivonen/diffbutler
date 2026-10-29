"""Forge-neutral, immutable read results. No persistence or workflow side effects."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


def identity_part(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in {".", ".."}:
        raise ValueError("repository, user, and team names must be simple identity components")
    return value


def repository_parts(value: str, *, allow_wildcard: bool = False) -> tuple[str, str]:
    parts = value.split("/")
    if len(parts) != 2:
        raise ValueError("repository must be namespace/name")
    owner, name = parts
    identity_part(owner)
    if not (allow_wildcard and name == "*"):
        identity_part(name)
    return owner, name


@dataclass(frozen=True)
class Account:
    forge: str
    login: str
    display_name: str


@dataclass(frozen=True)
class PRRef:
    forge: str
    namespace: str
    repository: str
    number: int

    def __post_init__(self) -> None:
        identity_part(self.namespace)
        identity_part(self.repository)
        if self.number < 1:
            raise ValueError("PR number must be positive")


def repository_matches(scope: str, ref: PRRef) -> bool:
    owner, name = repository_parts(scope, allow_wildcard=True)
    return owner.lower() == ref.namespace.lower() and (
        name == "*" or name.lower() == ref.repository.lower()
    )


@dataclass(frozen=True)
class PullRequest:
    ref: PRRef
    title: str
    author: str
    url: str
    state: Literal["open", "closed", "merged"]
    head_sha: str
    base_sha: str
    head_ref: str
    base_ref: str
    is_draft: bool
    created_at: datetime
    updated_at: datetime
    requested_users: tuple[str, ...]
    requested_teams: tuple[str, ...]
    additions: int | None = None
    deletions: int | None = None
    files: int | None = None
    labels: tuple[str, ...] = ()
    ci: str = "unknown"
    assignees: tuple[str, ...] = ()


@dataclass(frozen=True)
class PRQuery:
    repository: str | None = None
    author: str | None = None
    state: Literal["open", "closed", "merged", "all"] = "open"
    text: str = ""
    page_size: int = 30

    def __post_init__(self) -> None:
        if self.repository is not None:
            repository_parts(self.repository, allow_wildcard=True)
        if self.author is not None:
            identity_part(self.author)
        if self.state not in {"open", "closed", "merged", "all"}:
            raise ValueError("state must be open, closed, merged, or all")
        if not 1 <= self.page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")


@dataclass(frozen=True)
class PRPage:
    items: tuple[PullRequest, ...]
    next_cursor: str | None
    complete: bool = True
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestedPR:
    pull_request: PullRequest
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Discovery:
    items: tuple[RequestedPR, ...]
    complete: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TeamMembership:
    teams: tuple[str, ...]
    complete: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RateLimit:
    resource: str
    remaining: int
    limit: int
    reset_at: datetime


@dataclass(frozen=True)
class ForgeHealth:
    account: Account
    limits: tuple[RateLimit, ...]
    teams: TeamMembership | None
