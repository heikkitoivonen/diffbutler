"""Durable conversation identity and analysis attempts; no automatic dispatch."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_session",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("review_task_id", sa.Uuid(), sa.ForeignKey("review_task.id"), nullable=False),
        sa.Column("reviewer_id", sa.String(255), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(1024), nullable=False),
        sa.Column("backend_session_id", sa.String(1024)),
        sa.Column("repository_path", sa.String(4096)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("transcript_ref", sa.String(4096)),
        sa.Column("resource_manifest_json", sa.JSON(), nullable=False),
        sa.Column("cleanup_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resumed_at", sa.DateTime(timezone=True)),
        sa.Column("cleaned_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("review_task_id", "reviewer_id", "generation"),
        sa.UniqueConstraint("id", "review_task_id", "reviewer_id"),
        sa.CheckConstraint("generation > 0", name="positive_generation"),
        sa.CheckConstraint("status IN ('ready', 'suspended', 'cleaned')", name="session_status"),
    )
    op.create_table(
        "ai_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("review_task_id", sa.Uuid(), sa.ForeignKey("review_task.id"), nullable=False),
        sa.Column("review_session_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_id", sa.String(255), nullable=False),
        sa.Column("prompt_version", sa.String(255), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("analysis_key", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("parse_status", sa.String(32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("stdout_blob", sa.LargeBinary()),
        sa.Column("stderr_blob", sa.LargeBinary()),
        sa.UniqueConstraint("analysis_key", "attempt"),
        sa.ForeignKeyConstraint(
            ["review_session_id", "review_task_id", "reviewer_id"],
            ["review_session.id", "review_session.review_task_id", "review_session.reviewer_id"],
        ),
        sa.CheckConstraint("attempt > 0", name="positive_attempt"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'stale')",
            name="run_status",
        ),
        sa.CheckConstraint(
            "parse_status IN ('pending', 'succeeded', 'failed')", name="parse_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("ai_run")
    op.drop_table("review_session")
