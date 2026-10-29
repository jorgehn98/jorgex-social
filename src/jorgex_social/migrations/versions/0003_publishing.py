"""publishing

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29 19:51:43.780073
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jorgex_social.db

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("relative_path", sa.String(length=200), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=8), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("url_token", sa.String(length=64), nullable=True),
        sa.Column("url_expires_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("created_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("delete_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("deleted_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url_token"),
    )
    with op.batch_alter_table("media", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_media_delete_at"), ["delete_at"], unique=False)

    op.create_table(
        "posts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("caption", sa.Text(), nullable=False),
        sa.Column("cover_media_id", sa.String(length=36), nullable=True),
        sa.Column("thumb_offset_ms", sa.Integer(), nullable=True),
        sa.Column("scheduled_for", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("published_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("external_ref", sa.String(length=64), nullable=True),
        sa.Column("permalink", sa.String(length=500), nullable=True),
        sa.Column("failure", sa.String(length=32), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=500), nullable=True),
        sa.Column("created_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("updated_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["cover_media_id"],
            ["media.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("network", "external_ref"),
    )
    with op.batch_alter_table("posts", schema=None) as batch_op:
        batch_op.create_index(
            "ix_posts_status_scheduled_for", ["status", "scheduled_for"], unique=False
        )

    op.create_table(
        "post_items",
        sa.Column("post_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("media_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media.id"],
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["posts.id"],
        ),
        sa.PrimaryKeyConstraint("post_id", "position"),
    )
    op.create_table(
        "publish_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("post_id", sa.String(length=36), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("started_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("ended_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("container_ref", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.Column("error_subcode", sa.Integer(), nullable=True),
        sa.Column("resolved_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("resolved_as", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["posts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("post_id", "number"),
    )


def downgrade() -> None:
    op.drop_table("publish_attempts")
    op.drop_table("post_items")
    with op.batch_alter_table("posts", schema=None) as batch_op:
        batch_op.drop_index("ix_posts_status_scheduled_for")

    op.drop_table("posts")
    with op.batch_alter_table("media", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_media_delete_at"))

    op.drop_table("media")
