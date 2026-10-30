"""replies

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-30 03:16:13.073746
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jorgex_social.db

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("last_user_message_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("window_until", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("paused_until", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("paused_forever", sa.Boolean(), nullable=False),
        sa.Column("created_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("updated_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("purge_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("network", "external_id"),
    )
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_conversations_purge_at"), ["purge_at"], unique=False)

    op.create_table(
        "rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("media_ref", sa.String(length=64), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("activated_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("created_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("updated_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("archived_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("external_ref", sa.String(length=64), nullable=False),
        sa.Column("media_ref", sa.String(length=64), nullable=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("occurred_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("received_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("rule_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["rules.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("network", "type", "external_ref"),
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_events_received_at"), ["received_at"], unique=False)

    op.create_table(
        "deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("rule_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("window_until_at_send", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("result_ref", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.Column("error_subcode", sa.Integer(), nullable=True),
        sa.Column("created_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("attempted_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("finished_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["rules.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "event_id", "action"),
    )
    with op.batch_alter_table("deliveries", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_deliveries_attempted_at"), ["attempted_at"], unique=False
        )
        batch_op.create_index(
            "ix_deliveries_status_created_at", ["status", "created_at"], unique=False
        )
        batch_op.create_index(
            "uq_deliveries_private_reply",
            ["event_id"],
            unique=True,
            sqlite_where=sa.text("action = 'private_reply'"),
        )


def downgrade() -> None:
    with op.batch_alter_table("deliveries", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_deliveries_private_reply", sqlite_where=sa.text("action = 'private_reply'")
        )
        batch_op.drop_index("ix_deliveries_status_created_at")
        batch_op.drop_index(batch_op.f("ix_deliveries_attempted_at"))

    op.drop_table("deliveries")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_events_received_at"))

    op.drop_table("events")
    op.drop_table("rules")
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_conversations_purge_at"))
    op.drop_table("conversations")
