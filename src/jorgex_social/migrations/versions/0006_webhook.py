"""webhook

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30 13:13:24.805972
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jorgex_social.db

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("connections", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("webhook_notified_at", jorgex_social.db.UTCDateTime(length=32), nullable=True)
        )

    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("source", sa.String(length=16), server_default="poll", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_column("source")

    with op.batch_alter_table("connections", schema=None) as batch_op:
        batch_op.drop_column("webhook_notified_at")
