"""oauth

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-30 16:36:08.400346

The OAuth authorization server for remote MCP clients: registered clients, pending requests,
codes, grants and tokens (codes and tokens as hashes only).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jorgex_social.db

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("info", sa.JSON(), nullable=False),
        sa.Column("created_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("consented_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "oauth_grants",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("client_id", sa.String(length=36), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("resource", sa.String(length=300), nullable=False),
        sa.Column("created_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("revoked_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["oauth_clients.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("oauth_grants", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_oauth_grants_client_id"), ["client_id"], unique=False)

    op.create_table(
        "oauth_requests",
        sa.Column("id_hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=36), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("expires_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("session_hash", sa.String(length=64), nullable=True),
        sa.Column("nonce_hash", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["oauth_clients.id"],
        ),
        sa.PrimaryKeyConstraint("id_hash"),
    )
    with op.batch_alter_table("oauth_requests", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_oauth_requests_client_id"), ["client_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_requests_expires_at"), ["expires_at"], unique=False
        )

    op.create_table(
        "oauth_codes",
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=36), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("expires_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("used_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.Column("grant_id", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["oauth_clients.id"],
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["oauth_grants.id"],
        ),
        sa.PrimaryKeyConstraint("hash"),
    )
    with op.batch_alter_table("oauth_codes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_oauth_codes_client_id"), ["client_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_oauth_codes_expires_at"), ["expires_at"], unique=False)

    op.create_table(
        "oauth_tokens",
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("grant_id", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("parent_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("expires_at", jorgex_social.db.UTCDateTime(length=32), nullable=False),
        sa.Column("used_at", jorgex_social.db.UTCDateTime(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["oauth_grants.id"],
        ),
        sa.PrimaryKeyConstraint("hash"),
    )
    with op.batch_alter_table("oauth_tokens", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_oauth_tokens_expires_at"), ["expires_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_oauth_tokens_grant_id"), ["grant_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_oauth_tokens_parent_hash"), ["parent_hash"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("oauth_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oauth_tokens_parent_hash"))
        batch_op.drop_index(batch_op.f("ix_oauth_tokens_grant_id"))
        batch_op.drop_index(batch_op.f("ix_oauth_tokens_expires_at"))

    op.drop_table("oauth_tokens")
    with op.batch_alter_table("oauth_codes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oauth_codes_expires_at"))
        batch_op.drop_index(batch_op.f("ix_oauth_codes_client_id"))

    op.drop_table("oauth_codes")
    with op.batch_alter_table("oauth_requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oauth_requests_expires_at"))
        batch_op.drop_index(batch_op.f("ix_oauth_requests_client_id"))

    op.drop_table("oauth_requests")
    with op.batch_alter_table("oauth_grants", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oauth_grants_client_id"))

    op.drop_table("oauth_grants")
    op.drop_table("oauth_clients")
