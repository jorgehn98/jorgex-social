"""comment author

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-30 22:00:00.000000

The id of a comment's author, so a rule answers one person once per post and a pause reaches
their comments. Earlier comments stay without one: their author was never stored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("author_ref", sa.String(length=64), nullable=True))
        batch_op.create_index(
            "ix_events_network_media_ref_author_ref",
            ["network", "media_ref", "author_ref"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index("ix_events_network_media_ref_author_ref")
        batch_op.drop_column("author_ref")
