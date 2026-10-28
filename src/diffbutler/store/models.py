"""Identity, review-cycle history, and durable per-account monitoring."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    LargeBinary,
    MetaData,
    String,
    Text,
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
    snapshot_json: Mapped[dict[str, object] | None] = mapped_column(JSON)
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
    completed_head_sha: Mapped[str | None] = mapped_column(String(64))
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


class ForgeAccount(Base):
    """Full API-instance identity mapped to a ledger account; local stays separate."""

    __tablename__ = "forge_account"
    __table_args__ = (UniqueConstraint("forge", "login"),)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), primary_key=True)
    forge: Mapped[str] = mapped_column(String(255))
    login: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(default=0)


class PRMonitor(Base):
    """Monitoring persists independently of request membership and completed cycles."""

    __tablename__ = "pr_monitor"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), primary_key=True)
    pull_request_id: Mapped[UUID] = mapped_column(ForeignKey("pull_request.id"), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("review_task.id"))
    following: Mapped[bool] = mapped_column(default=True)
    explicit_review: Mapped[bool] = mapped_column(default=False, server_default="0")
    sources: Mapped[list[str]] = mapped_column(JSON, default=list)
    snapshot_json: Mapped[dict[str, object]] = mapped_column(JSON)
    activity_json: Mapped[str | None] = mapped_column(Text)
    refresh_pending: Mapped[bool] = mapped_column(default=False)
    notice: Mapped[str] = mapped_column(String(255), default="")
    request_key: Mapped[str] = mapped_column(String(1024), default="")
    review_key: Mapped[str] = mapped_column(String(1024), default="")


class ReviewSession(Base):
    """A conversation belongs to a task/account cycle, independently of its runs."""

    __tablename__ = "review_session"
    __table_args__ = (
        UniqueConstraint("review_task_id", "reviewer_id", "generation"),
        UniqueConstraint("id", "review_task_id", "reviewer_id"),
        CheckConstraint("generation > 0", name="positive_generation"),
        CheckConstraint("status IN ('ready', 'suspended', 'cleaned')", name="session_status"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    review_task_id: Mapped[UUID] = mapped_column(ForeignKey("review_task.id"))
    reviewer_id: Mapped[str] = mapped_column(String(255))
    generation: Mapped[int] = mapped_column(default=1)
    display_name: Mapped[str] = mapped_column(String(1024))
    backend_session_id: Mapped[str | None] = mapped_column(String(1024))
    repository_path: Mapped[str | None] = mapped_column(String(4096))
    status: Mapped[str] = mapped_column(String(32), default="ready")
    transcript_ref: Mapped[str | None] = mapped_column(String(4096))
    resource_manifest_json: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    cleanup_status: Mapped[str] = mapped_column(String(32), default="not_requested")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AIRun(Base):
    """Attempt metadata survives deletion of raw output and session resources."""

    __tablename__ = "ai_run"
    __table_args__ = (
        UniqueConstraint("analysis_key", "attempt"),
        ForeignKeyConstraint(
            ["review_session_id", "review_task_id", "reviewer_id"],
            ["review_session.id", "review_session.review_task_id", "review_session.reviewer_id"],
        ),
        CheckConstraint("attempt > 0", name="positive_attempt"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'stale')",
            name="run_status",
        ),
        CheckConstraint("parse_status IN ('pending', 'succeeded', 'failed')", name="parse_status"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    review_task_id: Mapped[UUID] = mapped_column(ForeignKey("review_task.id"))
    review_session_id: Mapped[UUID]
    reviewer_id: Mapped[str] = mapped_column(String(255))
    prompt_version: Mapped[str] = mapped_column(String(255))
    config_hash: Mapped[str] = mapped_column(String(64))
    head_sha: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str] = mapped_column(String(64))
    analysis_key: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int]
    status: Mapped[str] = mapped_column(String(32), default="queued")
    parse_status: Mapped[str] = mapped_column(String(32), default="pending")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None]
    status_reason: Mapped[str | None] = mapped_column(Text)
    stdout_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    stderr_blob: Mapped[bytes | None] = mapped_column(LargeBinary)


class RunClaim(Base):
    """Cancellation invalidates results without releasing a live worker's slot."""

    __tablename__ = "run_claim"
    token: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("ai_run.id"), unique=True)


class ReviewResult(Base):
    """Retained analysis is separate from cleanup-eligible raw AIRun output."""

    __tablename__ = "review_result"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("ai_run.id"), primary_key=True)
    document_json: Mapped[str | None] = mapped_column(Text)
    anchors_json: Mapped[str | None] = mapped_column(Text)
    parse_tier: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[list[str]] = mapped_column(JSON)
    selected_indices: Mapped[list[int]] = mapped_column(JSON)
    min_confidence: Mapped[float] = mapped_column(Float)
    findings_threshold: Mapped[str] = mapped_column(String(32))


class ForegroundLease(Base):
    __tablename__ = "foreground_lease"
    token: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("review_session.id"), unique=True)
    backend_session_id: Mapped[str] = mapped_column(String(1024))
    head_sha: Mapped[str] = mapped_column(String(128))
    task_version: Mapped[int] = mapped_column()
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CleanupClaim(Base):
    __tablename__ = "cleanup_claim"
    token: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("cleanup_job.id"), unique=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProcessLaunch(Base):
    """Historical ownership metadata; token references one of three claim types."""

    __tablename__ = "process_launch"
    __table_args__ = (
        CheckConstraint("kind IN ('background', 'foreground', 'cleanup')", name="operation_kind"),
        CheckConstraint("launcher_pid IS NULL OR launcher_pid > 0", name="positive_launcher_pid"),
    )
    token: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    host_id: Mapped[UUID] = mapped_column()
    boot_id: Mapped[UUID] = mapped_column()
    launcher_pid: Mapped[int | None] = mapped_column()


class ObservedActivity(Base):
    """Published forge evidence, deduplicated across polling and review cycles."""

    __tablename__ = "observed_activity"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), primary_key=True)
    pull_request_id: Mapped[UUID] = mapped_column(ForeignKey("pull_request.id"), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    review_task_id: Mapped[UUID] = mapped_column(ForeignKey("review_task.id"))
    payload_json: Mapped[str] = mapped_column(Text)


class UIPreference(Base):
    __tablename__ = "ui_preference"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
