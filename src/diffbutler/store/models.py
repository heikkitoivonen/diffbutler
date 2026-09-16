"""M0 identity and review-cycle ledger. No workflow transitions are implemented yet."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

LOCAL_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Account(Base):
    __tablename__ = "account"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    forge: Mapped[str] = mapped_column(String(32))
    login: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))


class Repository(Base):
    __tablename__ = "repository"
    __table_args__ = (UniqueConstraint("forge", "namespace", "name"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    forge: Mapped[str] = mapped_column(String(255))
    namespace: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    default_branch: Mapped[str] = mapped_column(String(255))
    config_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)


class PullRequest(Base):
    __tablename__ = "pull_request"
    __table_args__ = (UniqueConstraint("repository_id", "number"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repository.id"))
    number: Mapped[int]
    title: Mapped[str] = mapped_column(String(1024))
    author: Mapped[str] = mapped_column(String(255))
    head_sha: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str] = mapped_column(String(64))
    head_ref: Mapped[str] = mapped_column(String(255))
    base_ref: Mapped[str] = mapped_column(String(255))
    is_draft: Mapped[bool] = mapped_column(default=False)
    url: Mapped[str] = mapped_column(String(2048))
    state: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    extra_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class ReviewTask(Base):
    __tablename__ = "review_task"
    __table_args__ = (
        UniqueConstraint("pull_request_id", "account_id", "cycle_number"),
        CheckConstraint("cycle_number > 0", name="positive_cycle"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    pull_request_id: Mapped[UUID] = mapped_column(ForeignKey("pull_request.id"))
    account_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), default=LOCAL_ACCOUNT_ID)
    cycle_number: Mapped[int] = mapped_column(default=1)
    state: Mapped[str] = mapped_column(String(32), default="PENDING_AI_REVIEW")
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[int] = mapped_column(default=0)
    notes: Mapped[str | None] = mapped_column(String(16384))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    following: Mapped[bool] = mapped_column(default=True)
    request_sources_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_request_event: Mapped[str | None] = mapped_column(String(255))
    last_activity_cursor: Mapped[str | None] = mapped_column(String(1024))
    last_reviewed_sha: Mapped[str | None] = mapped_column(String(64))
    approved_sha: Mapped[str | None] = mapped_column(String(64))
    completion_reason: Mapped[str | None] = mapped_column(String(64))
    completion_source: Mapped[str | None] = mapped_column(String(64))
    completion_event_watermark: Mapped[str | None] = mapped_column(String(1024))
    action_version: Mapped[int] = mapped_column(default=0)
    state_source: Mapped[str | None] = mapped_column(String(64))
    state_event_watermark: Mapped[str | None] = mapped_column(String(1024))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CleanupJob(Base):
    __tablename__ = "cleanup_job"
    __table_args__ = (UniqueConstraint("review_task_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    review_task_id: Mapped[UUID] = mapped_column(ForeignKey("review_task.id"))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(4096))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
