"""insights snapshots

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29 19:37:56.174768
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jorgex_social.db

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_snapshots",
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("period_start", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("period_end", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("taken_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("followers", sa.Integer(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("network", "period_start", "period_end"),
    )
    op.create_table(
        "post_snapshots",
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("media_ref", sa.String(length=64), nullable=False),
        sa.Column("taken_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("product_type", sa.String(length=16), nullable=False),
        sa.Column("published_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("permalink", sa.String(length=500), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("network", "media_ref", "taken_at"),
    )


def downgrade() -> None:
    op.drop_table("post_snapshots")
    op.drop_table("account_snapshots")
