"""Retain original and validated anchors separately from model findings."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_result", sa.Column("anchors_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_result", "anchors_json")
