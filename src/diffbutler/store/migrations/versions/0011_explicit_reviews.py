"""Persist visibility overrides for explicitly selected review targets."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pr_monitor",
        sa.Column("explicit_review", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("pr_monitor", "explicit_review")
