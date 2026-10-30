"""rule post

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-30 23:30:00.000000

The post a rule waits for, so a rule can be prepared before Instagram gives the post a media id.
Earlier rules stay without one: they keep the scope they had.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("rules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("post_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key("fk_rules_post_id_posts", "posts", ["post_id"], ["id"])


def downgrade() -> None:
    # Without post_id a waiting rule would read as a rule for every post, so it stops for good.
    op.execute(
        "UPDATE rules SET active = 0, archived_at = COALESCE(archived_at, updated_at) "
        "WHERE post_id IS NOT NULL AND media_ref IS NULL"
    )
    with op.batch_alter_table("rules", schema=None) as batch_op:
        batch_op.drop_constraint("fk_rules_post_id_posts", type_="foreignkey")
        batch_op.drop_column("post_id")
