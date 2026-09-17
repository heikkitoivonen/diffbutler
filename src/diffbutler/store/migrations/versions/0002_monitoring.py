"""Account-scoped monitoring and persistent activity watermarks."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_task", sa.Column("completed_head_sha", sa.String(64), nullable=True))
    op.create_table(
        "forge_account",
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("account.id"), primary_key=True),
        sa.Column("forge", sa.String(255), nullable=False),
        sa.Column("login", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("forge", "login", name="uq_forge_account_forge"),
    )
    op.create_table(
        "pr_monitor",
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("account.id"), primary_key=True),
        sa.Column("pull_request_id", sa.Uuid(), sa.ForeignKey("pull_request.id"), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("review_task.id"), nullable=False),
        sa.Column("following", sa.Boolean(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("request_key", sa.String(1024), nullable=False),
        sa.Column("review_key", sa.String(1024), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("pr_monitor")
    op.drop_table("forge_account")
    op.drop_column("review_task", "completed_head_sha")
