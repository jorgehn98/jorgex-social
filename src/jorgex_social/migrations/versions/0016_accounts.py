"""accounts

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-17 10:00:00.000000

The account, the row of `connections`, replaces the network as the key of the domain: two accounts
of the same network can then live in one installation (decision 10). Every row of the domain gets
`account_id` and gives up `network`, the unique keys become per account, and the kill switch moves
from `settings` to the connection whose writes it stops.

Every existing row goes to the connection of its network. If a network has rows and no connection,
one is created for them: that is the simulation, whose rows must never mix with the real ones, and a
database that prepared posts before connecting anything.

`audit_log` is append-only, so its rows keep `account_id` null: nothing in them says which account
they were about, and the operator's sign-ins are no account's at all. The incident a sign-in opens
stays without an account too, since only `set-password` or `enroll-totp` close it.

Downgrading is meant to undo a fresh upgrade, not to run an installation on the old schema again. It
fails if two accounts (or two modes) share an `idempotency_key`, if two accounts share a network, or
if both have a post with the same media ref, because the old unique keys were global and per
network. It cannot re-seal a credential either: one re-sealed with its connection's context after
the upgrade only opens with `connect-instagram` again, since this schema has no master key.
"""

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jorgex_social.db

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Names for the constraints SQLite reflects without one, so they can be dropped by name. The ones
# this migration creates are named the same way.
NAMING = {"pk": "pk_%(table_name)s", "uq": "uq_%(table_name)s_%(column_0_N_name)s"}

# Tables that carry the network until this migration. Its rows name their own connection.
BY_NETWORK = (
    "posts",
    "rules",
    "events",
    "conversations",
    "post_snapshots",
    "account_snapshots",
    "recent_media",
    "channel_incidents",
)
# Every table whose rows belong to an account.
WITH_ACCOUNT = (*BY_NETWORK, "media", "deliveries")
UUID = sa.String(length=36)
NETWORK = sa.String(length=32)


def _row(bind, sql: str, **params):
    return bind.execute(sa.text(sql), params).first()


def _scalar(bind, sql: str, **params):
    return bind.execute(sa.text(sql), params).scalar()


def _add_account(table: str) -> None:
    """The column, nullable: the backfill fills it before it is made required."""
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.add_column(sa.Column("account_id", UUID, nullable=True))


def _assert_filled(table: str) -> None:
    leftover = _scalar(op.get_bind(), f"SELECT count(*) FROM {table} WHERE account_id IS NULL")
    if leftover:
        raise RuntimeError(f"{table} has {leftover} rows without an account")


def _require(batch_op, table: str) -> None:
    """Makes the filled column required and tied to the connection."""
    batch_op.alter_column("account_id", existing_type=UUID, nullable=False)
    batch_op.create_foreign_key(
        f"fk_{table}_account_id_connections", "connections", ["account_id"], ["id"]
    )


def _connection_of(bind, network: str) -> str:
    """The id of the connection of `network`, created if that network has rows and none.

    A connection made here has no external account id: it only owns the rows written before
    anything was connected, and the API keeps reporting the account as missing."""
    found = _scalar(
        bind, "SELECT id FROM connections WHERE network = :network LIMIT 1", network=network
    )
    if found is not None:
        return found
    connection_id = str(uuid.uuid7())
    bind.execute(
        sa.text(
            "INSERT INTO connections (id, network, external_account_id, username, status)"
            " VALUES (:id, :network, '', '', 'ok')"
        ),
        {"id": connection_id, "network": network},
    )
    return connection_id


def _fill_from_network(table: str, keep_without_account: str | None = None) -> None:
    """Gives each row the connection of the network it names. The rows of `keep_without_account`
    belong to the operator, one for the whole installation, so they stay without an account."""
    query = (
        f"UPDATE {table} SET account_id = (SELECT id FROM connections c"
        f" WHERE c.network = {table}.network)"
    )
    if keep_without_account is not None:
        query += f" WHERE {table}.type <> '{keep_without_account}'"
    op.execute(query)


def _drop_foreign_key(batch_op, table: str) -> None:
    batch_op.drop_constraint(f"fk_{table}_account_id_connections", type_="foreignkey")


def upgrade() -> None:
    bind = op.get_bind()

    # The account: no longer one per network, and it owns the kill switch.
    with op.batch_alter_table("connections", schema=None, naming_convention=NAMING) as batch_op:
        batch_op.add_column(
            sa.Column("kill_switch", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("kill_switch_updated_at", jorgex_social.db.UTCDateTime(length=32))
        )
        batch_op.add_column(
            sa.Column("credential_legacy", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.drop_constraint("uq_connections_network", type_="unique")
        batch_op.create_unique_constraint(
            "uq_connections_network_external_account_id", ["network", "external_account_id"]
        )
    # The rows whose ciphertext is still sealed with the network's context: the only ones allowed to
    # open it, and only until they are sealed again (account/credential.py).
    op.execute(
        "UPDATE connections SET credential_legacy = 1 WHERE credential_encrypted IS NOT NULL"
    )

    # One connection per network with rows, before any row needs its id and before the
    # installation's switch goes to them.
    for table in BY_NETWORK:
        for network in bind.execute(sa.text(f"SELECT DISTINCT network FROM {table}")).scalars():
            if network is not None:
                _connection_of(bind, network)

    stored = _row(bind, "SELECT value, updated_at FROM settings WHERE key = 'kill_switch'")
    if stored is not None:
        # It was the installation's switch, so every account there is, the ones just created too,
        # had it on or off.
        enabled = json.loads(stored.value) if isinstance(stored.value, str) else bool(stored.value)
        op.execute(
            sa.text(
                "UPDATE connections SET kill_switch = :on, kill_switch_updated_at = :at"
            ).bindparams(on=enabled, at=stored.updated_at)
        )
    op.execute("DELETE FROM settings WHERE key = 'kill_switch'")

    # Posts: the account indexes them, and the idempotency key is the caller's, not the world's.
    _add_account("posts")
    _fill_from_network("posts")
    _assert_filled("posts")
    with op.batch_alter_table("posts", schema=None, naming_convention=NAMING) as batch_op:
        batch_op.drop_constraint("uq_posts_idempotency_key", type_="unique")
        batch_op.drop_column("network")
        _require(batch_op, "posts")
        batch_op.create_unique_constraint(
            "uq_posts_account_id_external_ref", ["account_id", "external_ref"]
        )
        batch_op.create_unique_constraint(
            "uq_posts_account_id_idempotency_key", ["account_id", "idempotency_key"]
        )

    # Media belongs to the account of the post that uses it; media no post uses is purged on its
    # own, and gets the account of the database.
    _add_account("media")
    op.execute(
        "UPDATE media SET account_id = COALESCE("
        "(SELECT p.account_id FROM post_items i JOIN posts p ON p.id = i.post_id"
        " WHERE i.media_id = media.id AND p.account_id IS NOT NULL LIMIT 1),"
        "(SELECT p.account_id FROM posts p"
        " WHERE p.cover_media_id = media.id AND p.account_id IS NOT NULL LIMIT 1),"
        "(SELECT c.id FROM connections c ORDER BY c.id LIMIT 1))"
    )
    if _scalar(bind, "SELECT count(*) FROM media WHERE account_id IS NULL"):
        # Uploaded before anything was connected, in a mode the database does not record: no
        # domain row names it, so the account it lands on changes nothing that is queried.
        if not _scalar(bind, "SELECT count(*) FROM connections"):
            _connection_of(bind, "instagram")
        op.execute(
            "UPDATE media SET account_id = (SELECT c.id FROM connections c ORDER BY c.id LIMIT 1)"
        )
    _assert_filled("media")
    with op.batch_alter_table("media", schema=None) as batch_op:
        _require(batch_op, "media")

    # Rules: they had no unique key of their own.
    _add_account("rules")
    _fill_from_network("rules")
    _assert_filled("rules")
    with op.batch_alter_table("rules", schema=None) as batch_op:
        batch_op.drop_column("network")
        _require(batch_op, "rules")

    # Events: one row per event of an account.
    _add_account("events")
    _fill_from_network("events")
    _assert_filled("events")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index("ix_events_network_media_ref_author_ref")
        batch_op.drop_column("network")
        _require(batch_op, "events")
        batch_op.create_index(
            "ix_events_account_id_media_ref_author_ref", ["account_id", "media_ref", "author_ref"]
        )
        batch_op.create_unique_constraint(
            "uq_events_account_id_type_external_ref", ["account_id", "type", "external_ref"]
        )

    # Deliveries: the account comes from their event, and the hourly caps count them per account.
    _add_account("deliveries")
    op.execute(
        "UPDATE deliveries SET account_id ="
        " (SELECT e.account_id FROM events e WHERE e.id = deliveries.event_id)"
    )
    _assert_filled("deliveries")
    with op.batch_alter_table("deliveries", schema=None) as batch_op:
        _require(batch_op, "deliveries")

    # Conversations: one per person and account.
    _add_account("conversations")
    _fill_from_network("conversations")
    _assert_filled("conversations")
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_column("network")
        _require(batch_op, "conversations")
        batch_op.create_unique_constraint(
            "uq_conversations_account_id_external_id", ["account_id", "external_id"]
        )

    # Incidents: per account, except the operator's own, which is no account's.
    # Incidents: per account, except the operator's own, which has none. Assigning one to the
    # connection would leave a sign-in warning that only the credential commands close, and they
    # close the ones without an account.
    _add_account("channel_incidents")
    _fill_from_network("channel_incidents", keep_without_account="sign_in_password_accepted")
    # The old index was per network, so two modes could each hold an open one; this one is per type.
    # The newest stays open, and the others close when it opened.
    op.execute(
        "UPDATE channel_incidents SET closed_at = ("
        " SELECT max(c.opened_at) FROM channel_incidents c"
        " WHERE c.type = channel_incidents.type AND c.account_id IS NULL AND c.closed_at IS NULL)"
        " WHERE account_id IS NULL AND closed_at IS NULL AND id NOT IN ("
        " SELECT c.id FROM channel_incidents c"
        " WHERE c.type = channel_incidents.type AND c.account_id IS NULL AND c.closed_at IS NULL"
        " ORDER BY c.opened_at DESC, c.id DESC LIMIT 1)"
    )
    with op.batch_alter_table("channel_incidents", schema=None) as batch_op:
        batch_op.drop_index("uq_channel_incidents_open", sqlite_where=sa.text("closed_at IS NULL"))
        batch_op.drop_column("network")
        batch_op.create_index(
            "uq_channel_incidents_open",
            ["account_id", "type"],
            unique=True,
            sqlite_where=sa.text("closed_at IS NULL"),
        )
        # SQLite takes one NULL as different from another, so the operator's need their own.
        batch_op.create_index(
            "uq_channel_incidents_operator",
            ["type"],
            unique=True,
            sqlite_where=sa.text("account_id IS NULL AND closed_at IS NULL"),
        )
        batch_op.create_foreign_key(
            "fk_channel_incidents_account_id_connections", "connections", ["account_id"], ["id"]
        )

    # The snapshots and the recent media are keyed by account: two accounts of one network measure
    # the same media refs.
    for table, key in (
        ("post_snapshots", ["account_id", "media_ref", "taken_at"]),
        ("account_snapshots", ["account_id", "period_start", "period_end"]),
        ("recent_media", ["account_id"]),
    ):
        _add_account(table)
        _fill_from_network(table)
        _assert_filled(table)
        with op.batch_alter_table(table, schema=None, naming_convention=NAMING) as batch_op:
            batch_op.drop_constraint(f"pk_{table}", type_="primary")
            batch_op.drop_column("network")
            _require(batch_op, table)
            batch_op.create_primary_key(f"pk_{table}", key)

    # Append-only: the triggers survive adding a column, which SQLite does in place. Historical
    # rows keep it null; like `object_id`, it carries no foreign key, so the log outlives what it
    # names.
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.add_column(sa.Column("account_id", UUID, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()

    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_column("account_id")
    # Dropping a column recreates the table, and the triggers with it: the log is still
    # append-only, with the exception 0013 made for denied sign-ins.
    for trigger in ("audit_log_no_update", "audit_log_no_delete"):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute(
        "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log "
        "WHEN NOT (OLD.action = 'operator.login' AND OLD.outcome = 'denied') "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
    )

    for table, key in (
        ("recent_media", ["network"]),
        ("account_snapshots", ["network", "period_start", "period_end"]),
        ("post_snapshots", ["network", "media_ref", "taken_at"]),
    ):
        with op.batch_alter_table(table, schema=None, naming_convention=NAMING) as batch_op:
            batch_op.drop_constraint(f"pk_{table}", type_="primary")
            batch_op.add_column(sa.Column("network", NETWORK, nullable=True))
        op.execute(
            f"UPDATE {table} SET network = (SELECT c.network FROM connections c"
            f" WHERE c.id = {table}.account_id)"
        )
        with op.batch_alter_table(table, schema=None, naming_convention=NAMING) as batch_op:
            _drop_foreign_key(batch_op, table)
            batch_op.drop_column("account_id")
            batch_op.alter_column("network", existing_type=NETWORK, nullable=False)
            batch_op.create_primary_key(f"pk_{table}", key)

    for table in ("channel_incidents", "conversations", "events", "rules"):
        with op.batch_alter_table(table, schema=None, naming_convention=NAMING) as batch_op:
            _drop_foreign_key(batch_op, table)
            batch_op.add_column(sa.Column("network", NETWORK, nullable=True))
        op.execute(
            f"UPDATE {table} SET network = (SELECT c.network FROM connections c"
            f" WHERE c.id = {table}.account_id)"
        )

    with op.batch_alter_table("channel_incidents", schema=None) as batch_op:
        batch_op.drop_index("uq_channel_incidents_open", sqlite_where=sa.text("closed_at IS NULL"))
        batch_op.drop_index(
            "uq_channel_incidents_operator",
            sqlite_where=sa.text("account_id IS NULL AND closed_at IS NULL"),
        )
    # The operator's incidents have no account. The installation's network names them; with no
    # connection at all there is no network to name, and the old schema cannot hold them.
    op.execute(
        "UPDATE channel_incidents SET network ="
        " (SELECT c.network FROM connections c ORDER BY c.id LIMIT 1) WHERE network IS NULL"
    )
    op.execute("DELETE FROM channel_incidents WHERE network IS NULL")
    with op.batch_alter_table("channel_incidents", schema=None) as batch_op:
        batch_op.alter_column("network", existing_type=NETWORK, nullable=False)
        batch_op.drop_column("account_id")
        batch_op.create_index(
            "uq_channel_incidents_open",
            ["network", "type"],
            unique=True,
            sqlite_where=sa.text("closed_at IS NULL"),
        )

    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_constraint("uq_conversations_account_id_external_id", type_="unique")
        batch_op.alter_column("network", existing_type=NETWORK, nullable=False)
        batch_op.drop_column("account_id")
        batch_op.create_unique_constraint(
            "uq_conversations_network_external_id", ["network", "external_id"]
        )

    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index("ix_events_account_id_media_ref_author_ref")
        batch_op.drop_constraint("uq_events_account_id_type_external_ref", type_="unique")
        batch_op.alter_column("network", existing_type=NETWORK, nullable=False)
        batch_op.drop_column("account_id")
        batch_op.create_index(
            "ix_events_network_media_ref_author_ref", ["network", "media_ref", "author_ref"]
        )
        batch_op.create_unique_constraint(
            "uq_events_network_type_external_ref", ["network", "type", "external_ref"]
        )

    with op.batch_alter_table("rules", schema=None) as batch_op:
        batch_op.alter_column("network", existing_type=NETWORK, nullable=False)
        batch_op.drop_column("account_id")

    # Media and deliveries had no network of their own.
    for table in ("media", "deliveries"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            _drop_foreign_key(batch_op, table)
            batch_op.drop_column("account_id")

    with op.batch_alter_table("posts", schema=None, naming_convention=NAMING) as batch_op:
        _drop_foreign_key(batch_op, "posts")
        batch_op.drop_constraint("uq_posts_account_id_external_ref", type_="unique")
        batch_op.drop_constraint("uq_posts_account_id_idempotency_key", type_="unique")
        batch_op.add_column(sa.Column("network", NETWORK, nullable=True))
    op.execute(
        "UPDATE posts SET network = (SELECT c.network FROM connections c"
        " WHERE c.id = posts.account_id)"
    )
    with op.batch_alter_table("posts", schema=None, naming_convention=NAMING) as batch_op:
        batch_op.alter_column("network", existing_type=NETWORK, nullable=False)
        batch_op.drop_column("account_id")
        batch_op.create_unique_constraint("uq_posts_idempotency_key", ["idempotency_key"])
        batch_op.create_unique_constraint(
            "uq_posts_network_external_ref", ["network", "external_ref"]
        )

    # The kill switch becomes the installation's again, with the value of the accounts there are.
    stored = _row(
        bind,
        "SELECT kill_switch, kill_switch_updated_at FROM connections"
        " WHERE kill_switch_updated_at IS NOT NULL ORDER BY id LIMIT 1",
    )
    if stored is not None:
        op.execute(
            sa.text(
                "INSERT INTO settings (key, value, updated_at) VALUES ('kill_switch', :on, :at)"
                " ON CONFLICT(key) DO UPDATE SET value = :on, updated_at = :at"
            ).bindparams(
                on="true" if stored.kill_switch else "false", at=stored.kill_switch_updated_at
            )
        )
    with op.batch_alter_table("connections", schema=None, naming_convention=NAMING) as batch_op:
        batch_op.drop_constraint("uq_connections_network_external_account_id", type_="unique")
        batch_op.create_unique_constraint("uq_connections_network", ["network"])
        batch_op.drop_column("credential_legacy")
        batch_op.drop_column("kill_switch_updated_at")
        batch_op.drop_column("kill_switch")
