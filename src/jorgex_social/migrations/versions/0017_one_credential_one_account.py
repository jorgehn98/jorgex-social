"""one credential, one account

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-17 12:00:00.000000

Every API token and every OAuth grant gains the one account it works on, required: the API takes
the account of a call from its credential and from nothing else (decision 10). Existing credentials
get the installation's account: the only connection there is, or the one that is not the
simulation's. A database with no connection at all gets the one a first write would have created,
which owns what it writes until `connect-instagram` fills it.

When the credentials of one database point at more than one account (several connections, none of
them the simulation's), there is no right answer and the migration stops instead of guessing.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jorgex_social.db

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = sa.String(length=36)
# The network of the account a first write creates, and of the simulated one (instagram/fake.py).
REAL_NETWORK = "instagram"
SIMULATION_NETWORK = "simulation"
# The tables that gain an account: one credential, one account.
CREDENTIALS = ("api_tokens", "oauth_grants")


def _scalar(bind, sql: str, **params):
    return bind.execute(sa.text(sql), params).scalar()


def _new_account(bind) -> str:
    """A connection nobody has connected yet: what a first write creates, so a database with
    credentials but no account keeps working as it did before this migration."""
    connection_id = str(uuid.uuid7())
    bind.execute(
        sa.text(
            "INSERT INTO connections (id, network, external_account_id, username, status)"
            " VALUES (:id, :network, '', '', 'ok')"
        ),
        {"id": connection_id, "network": REAL_NETWORK},
    )
    return connection_id


def _the_account(bind) -> str:
    """The account every existing credential goes to: the only connection there is, or the one
    that is not the simulation's, or a new one when there is no connection at all."""
    connections = bind.execute(sa.text("SELECT id, network FROM connections ORDER BY id")).all()
    if not connections:
        return _new_account(bind)
    if len(connections) == 1:
        return connections[0].id
    real = [connection.id for connection in connections if connection.network != SIMULATION_NETWORK]
    if len(real) == 1:
        return real[0]
    raise RuntimeError(
        f"cannot tell which account these credentials belong to: {len(connections)} connections "
        f"and {len(real)} of them not the simulation's. Bind them by hand and run this migration "
        "again."
    )


def _add_account(table: str) -> None:
    """The column, nullable: the backfill fills it before it is made required."""
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.add_column(sa.Column("account_id", UUID, nullable=True))


def _fill(table: str, account_id: str) -> None:
    op.execute(sa.text(f"UPDATE {table} SET account_id = :account").bindparams(account=account_id))
    leftover = _scalar(op.get_bind(), f"SELECT count(*) FROM {table} WHERE account_id IS NULL")
    if leftover:
        raise RuntimeError(f"{table} has {leftover} rows without an account")


def _require(batch_op, table: str) -> None:
    batch_op.alter_column("account_id", existing_type=UUID, nullable=False)
    batch_op.create_foreign_key(
        f"fk_{table}_account_id_connections", "connections", ["account_id"], ["id"]
    )


def upgrade() -> None:
    bind = op.get_bind()
    for table in CREDENTIALS:
        _add_account(table)
    # A fresh install has no credential to bind, and must not gain a connection it never made.
    if any(_scalar(bind, f"SELECT count(*) FROM {table}") for table in CREDENTIALS):
        account = _the_account(bind)
        for table in CREDENTIALS:
            _fill(table, account)
    for table in CREDENTIALS:
        with op.batch_alter_table(table, schema=None) as batch_op:
            _require(batch_op, table)


def downgrade() -> None:
    for table in reversed(CREDENTIALS):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(f"fk_{table}_account_id_connections", type_="foreignkey")
            batch_op.drop_column("account_id")
