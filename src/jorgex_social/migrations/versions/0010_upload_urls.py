"""upload urls

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-30 16:54:37.075148

One-use upload URLs for remote agents (token hashes only), tied to an OAuth grant.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jorgex_social.db

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upload_urls",
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("grant_id", sa.String(length=16), nullable=False),
        sa.Column("created_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("expires_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("used_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["oauth_grants.id"],
        ),
        sa.PrimaryKeyConstraint("hash"),
    )
    with op.batch_alter_table("upload_urls", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_upload_urls_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_upload_urls_grant_id"), ["grant_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("upload_urls", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_upload_urls_grant_id"))
        batch_op.drop_index(batch_op.f("ix_upload_urls_created_at"))

    op.drop_table("upload_urls")
