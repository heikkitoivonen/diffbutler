"""Retain parsed analysis independently of cleanup-eligible process output."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_result",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("ai_run.id"), primary_key=True),
        sa.Column("document_json", sa.Text(), nullable=True),
        sa.Column("parse_tier", sa.String(32), nullable=True),
        sa.Column("error", sa.String(255), nullable=True),
        sa.Column("notes", sa.JSON(), nullable=False),
        sa.Column("selected_indices", sa.JSON(), nullable=False),
        sa.Column("min_confidence", sa.Float(), nullable=False),
        sa.Column("findings_threshold", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("review_result")
