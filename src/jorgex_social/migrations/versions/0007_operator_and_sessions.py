"""operator and sessions

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-30 14:09:41.934824
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jorgex_social.db

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("singleton", sa.Integer(), nullable=False),
        sa.Column("password_hash", sa.String(length=200), nullable=False),
        sa.Column("totp_secret_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("totp_last_step", sa.Integer(), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("created_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.CheckConstraint("singleton = 1", name="ck_operator_single_row"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("singleton"),
    )
    op.create_table(
        "sessions",
        sa.Column("id_hash", sa.String(length=64), nullable=False),
        sa.Column("operator_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("expires_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("last_activity_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["operator.id"],
        ),
        sa.PrimaryKeyConstraint("id_hash"),
    )
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sessions_expires_at"), ["expires_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sessions_expires_at"))

    op.drop_table("sessions")
    op.drop_table("operator")
