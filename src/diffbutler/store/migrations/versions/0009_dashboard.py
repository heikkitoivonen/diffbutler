"""Retained forge receipts, cycle snapshots, and dashboard preferences."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_task", sa.Column("snapshot_json", sa.JSON(), nullable=True))
    op.add_column("pr_monitor", sa.Column("activity_json", sa.Text(), nullable=True))
    op.add_column(
        "pr_monitor",
        sa.Column("refresh_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "pr_monitor", sa.Column("notice", sa.String(255), nullable=False, server_default="")
    )
    op.create_table(
        "observed_activity",
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("account.id"), primary_key=True),
        sa.Column("pull_request_id", sa.Uuid(), sa.ForeignKey("pull_request.id"), primary_key=True),
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("review_task_id", sa.Uuid(), sa.ForeignKey("review_task.id"), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "ui_preference",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ui_preference")
    op.drop_table("observed_activity")
    op.drop_column("pr_monitor", "notice")
    op.drop_column("pr_monitor", "refresh_pending")
    op.drop_column("pr_monitor", "activity_json")
    op.drop_column("review_task", "snapshot_json")
