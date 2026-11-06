"""post trial

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-06 18:00:00.000000

Whether a reel goes out as a trial reel, shown only to non-followers at first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("posts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("trial", sa.String(length=8), nullable=True))


def downgrade() -> None:
    # Without the column a scheduled trial reel would go out to followers too: back to draft.
    op.execute(
        "UPDATE posts SET status = 'draft', scheduled_for = NULL "
        "WHERE trial IS NOT NULL AND status = 'scheduled'"
    )
    with op.batch_alter_table("posts", schema=None) as batch_op:
        batch_op.drop_column("trial")
