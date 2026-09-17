"""Validate GitHub response shapes before exposing immutable domain values."""

from datetime import datetime
from typing import cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from diffbutler.domain.forge import PRRef, PullRequest
from diffbutler.forges.base import ForgeError


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class User(ResponseModel):
    login: str
    name: str | None = None


class Organization(ResponseModel):
    login: str


class Team(ResponseModel):
    slug: str
    organization: Organization | None = None


class Repository(ResponseModel):
    full_name: str


class Branch(ResponseModel):
    sha: str
    ref: str
    repo: Repository | None


class Pull(ResponseModel):
    number: int = Field(gt=0)
    title: str
    user: User | None
    state: str
    merged: bool
    draft: bool = False
    head: Branch
    base: Branch
    # HTTP JSON dates are strings. Parse them separately rather than coercing
    # malformed booleans, IDs, or list contents via non-strict model validation.
    created_at: str
    updated_at: str
    requested_reviewers: list[User]
    requested_teams: list[Team]


class DateValue(BaseModel):
    value: AwareDatetime


def timestamp(value: str) -> datetime:
    return DateValue.model_validate({"value": value}).value


class SearchHit(ResponseModel):
    pull_request: dict[str, str | None]


class SearchResult(ResponseModel):
    total_count: int = Field(ge=0)
    incomplete_results: bool
    items: list[SearchHit]


def parse[T: BaseModel](model: type[T], value: object) -> T:
    try:
        return model.model_validate(value)
    except ValidationError:
        raise ForgeError("GitHub returned an unexpected response shape") from None


def objects(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ForgeError("GitHub returned an unexpected list response")
    return cast(list[object], value)


def pull_request(value: object, *, forge: str, web_base: str) -> PullRequest:
    data = parse(Pull, value)
    try:
        if data.base.repo is None:
            raise ValueError("base repository missing")
        owner, name = data.base.repo.full_name.split("/")
        ref = PRRef(forge, owner.lower(), name.lower(), data.number)
        if data.state not in {"open", "closed"}:
            raise ValueError("unknown pull request state")
        return PullRequest(
            ref=ref,
            title=data.title,
            author=data.user.login if data.user else "[deleted]",
            url=f"{web_base}/{owner}/{name}/pull/{data.number}",
            state="merged" if data.merged else ("closed" if data.state == "closed" else "open"),
            head_sha=data.head.sha,
            base_sha=data.base.sha,
            head_ref=data.head.ref,
            base_ref=data.base.ref,
            is_draft=data.draft,
            created_at=timestamp(data.created_at),
            updated_at=timestamp(data.updated_at),
            requested_users=tuple(user.login.lower() for user in data.requested_reviewers),
            requested_teams=tuple(
                f"{owner.lower()}/{team.slug.lower()}" for team in data.requested_teams
            ),
        )
    except ValueError:
        raise ForgeError("GitHub returned invalid pull request identity or dates") from None


class IssueEvent(ResponseModel):
    id: int
    event: str
    created_at: str
    requested_reviewer: User | None = None
    requested_team: Team | None = None


class Review(ResponseModel):
    id: int
    user: User | None
    state: str
    submitted_at: str | None = None
    commit_id: str | None = None
