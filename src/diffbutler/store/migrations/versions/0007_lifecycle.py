"""Durable foreground ownership and exclusive cleanup claims."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "foreground_lease",
        sa.Column("token", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_id", sa.Uuid(), sa.ForeignKey("review_session.id"), nullable=False, unique=True
        ),
        sa.Column("backend_session_id", sa.String(1024), nullable=False),
        sa.Column("head_sha", sa.String(128), nullable=False),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "cleanup_claim",
        sa.Column("token", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id", sa.Uuid(), sa.ForeignKey("cleanup_job.id"), nullable=False, unique=True
        ),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("cleanup_claim")
    op.drop_table("foreground_lease")
