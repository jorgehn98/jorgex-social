"""no operator lockout

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-30 18:00:00.000000

Sign-in guessing is now limited per client address; a lockout of the one account would let anyone
keep the operator out.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jorgex_social.db

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("operator", schema=None) as batch_op:
        batch_op.drop_column("locked_until")
        batch_op.drop_column("failed_attempts")


def downgrade() -> None:
    with op.batch_alter_table("operator", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("locked_until", jorgex_social.db.UTCDateTime(length=32), nullable=True)
        )
