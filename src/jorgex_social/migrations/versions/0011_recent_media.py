"""recent media

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-30 20:00:00.000000

The account's newest feed posts and reels as the worker last listed them (no captions).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jorgex_social.db

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recent_media",
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("network"),
    )


def downgrade() -> None:
    op.drop_table("recent_media")
