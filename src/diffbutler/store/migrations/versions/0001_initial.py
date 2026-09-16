"""Initial local identity and review-cycle ledger."""

from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("forge", sa.String(length=32), nullable=False),
        sa.Column("login", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account")),
    )
    op.create_table(
        "repository",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("forge", sa.String(length=255), nullable=False),
        sa.Column("namespace", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("default_branch", sa.String(length=255), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repository")),
        sa.UniqueConstraint("forge", "namespace", "name", name=op.f("uq_repository_forge")),
    )
    op.create_table(
        "pull_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("base_sha", sa.String(length=64), nullable=False),
        sa.Column("head_ref", sa.String(length=255), nullable=False),
        sa.Column("base_ref", sa.String(length=255), nullable=False),
        sa.Column("is_draft", sa.Boolean(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extra_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repository.id"],
            name=op.f("fk_pull_request_repository_id_repository"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pull_request")),
        sa.UniqueConstraint("repository_id", "number", name=op.f("uq_pull_request_repository_id")),
    )
    op.create_table(
        "review_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pull_request_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("cycle_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(length=16384), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("following", sa.Boolean(), nullable=False),
        sa.Column("request_sources_json", sa.JSON(), nullable=False),
        sa.Column("last_request_event", sa.String(length=255), nullable=True),
        sa.Column("last_activity_cursor", sa.String(length=1024), nullable=True),
        sa.Column("last_reviewed_sha", sa.String(length=64), nullable=True),
        sa.Column("approved_sha", sa.String(length=64), nullable=True),
        sa.Column("completion_reason", sa.String(length=64), nullable=True),
        sa.Column("completion_source", sa.String(length=64), nullable=True),
        sa.Column("completion_event_watermark", sa.String(length=1024), nullable=True),
        sa.Column("action_version", sa.Integer(), nullable=False),
        sa.Column("state_source", sa.String(length=64), nullable=True),
        sa.Column("state_event_watermark", sa.String(length=1024), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("cycle_number > 0", name=op.f("ck_review_task_positive_cycle")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["account.id"], name=op.f("fk_review_task_account_id_account")
        ),
        sa.ForeignKeyConstraint(
            ["pull_request_id"],
            ["pull_request.id"],
            name=op.f("fk_review_task_pull_request_id_pull_request"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_task")),
        sa.UniqueConstraint(
            "pull_request_id",
            "account_id",
            "cycle_number",
            name=op.f("uq_review_task_pull_request_id"),
        ),
    )
    op.create_table(
        "cleanup_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=4096), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["review_task_id"],
            ["review_task.id"],
            name=op.f("fk_cleanup_job_review_task_id_review_task"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cleanup_job")),
        sa.UniqueConstraint("review_task_id", name=op.f("uq_cleanup_job_review_task_id")),
    )
    op.bulk_insert(
        sa.table(
            "account",
            sa.column("id", sa.Uuid()),
            sa.column("forge", sa.String()),
            sa.column("login", sa.String()),
            sa.column("display_name", sa.String()),
        ),
        [
            {
                "id": UUID("00000000-0000-0000-0000-000000000001"),
                "forge": "local",
                "login": "local",
                "display_name": "Local account",
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("cleanup_job")
    op.drop_table("review_task")
    op.drop_table("pull_request")
    op.drop_table("repository")
    op.drop_table("account")
