"""purge denied sign-ins

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-30 22:00:00.000000

The audit log's delete trigger now lets denied sign-ins through, the one row anyone on the internet
can make it write, so they can be purged. Every other entry stays undeletable.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_trigger(condition: str) -> None:
    op.execute("DROP TRIGGER audit_log_no_delete")
    op.execute(
        f"CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log {condition} "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
    )


def upgrade() -> None:
    _replace_trigger("WHEN NOT (OLD.action = 'operator.login' AND OLD.outcome = 'denied')")


def downgrade() -> None:
    _replace_trigger("")
