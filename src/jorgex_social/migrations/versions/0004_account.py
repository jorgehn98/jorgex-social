"""account

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29 20:20:47.225395
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jorgex_social.db

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("occurred_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_ref", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=200), nullable=False),
        sa.Column("object_type", sa.String(length=32), nullable=True),
        sa.Column("object_id", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.create_index("ix_audit_log_object", ["object_type", "object_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_audit_log_occurred_at"), ["occurred_at"], unique=False)

    op.create_table(
        "channel_incidents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("opened_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("closed_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("acknowledged_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("channel_incidents", schema=None) as batch_op:
        batch_op.create_index(
            "ix_channel_incidents_type_closed_at", ["type", "closed_at"], unique=False
        )
        batch_op.create_index(
            "uq_channel_incidents_open",
            ["network", "type"],
            unique=True,
            sqlite_where=sa.text("closed_at IS NULL"),
        )

    op.create_table(
        "connections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("external_account_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=True),
        sa.Column("credential_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("credential_expires_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column(
            "credential_refreshed_at", jorgex_social.db.UTCDateTime(length=32), nullable=True
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=True),
        sa.Column("checked_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("network"),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    # The audit log only grows: the database itself refuses to change or remove an entry.
    for statement in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER audit_log_no_{statement.lower()} BEFORE {statement} ON audit_log "
            "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
        )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("connections")
    with op.batch_alter_table("channel_incidents", schema=None) as batch_op:
        batch_op.drop_index("uq_channel_incidents_open", sqlite_where=sa.text("closed_at IS NULL"))
        batch_op.drop_index("ix_channel_incidents_type_closed_at")

    op.drop_table("channel_incidents")
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_log_occurred_at"))
        batch_op.drop_index("ix_audit_log_object")

    op.execute("DROP TRIGGER audit_log_no_update")
    op.execute("DROP TRIGGER audit_log_no_delete")
    op.drop_table("audit_log")
