"""Durable exclusive worker ownership, separate from analysis validity."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_claim",
        sa.Column("token", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("ai_run.id"), nullable=False),
        sa.UniqueConstraint("run_id"),
    )


def downgrade() -> None:
    op.drop_table("run_claim")
