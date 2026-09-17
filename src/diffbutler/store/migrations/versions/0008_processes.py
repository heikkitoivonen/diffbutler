"""Durable pre-launch identity for conservative restart recovery."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "process_launch",
        sa.Column("token", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("boot_id", sa.Uuid(), nullable=False),
        sa.Column("launcher_pid", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('background', 'foreground', 'cleanup')", name="operation_kind"
        ),
        sa.CheckConstraint(
            "launcher_pid IS NULL OR launcher_pid > 0", name="positive_launcher_pid"
        ),
    )


def downgrade() -> None:
    op.drop_table("process_launch")
