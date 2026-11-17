"""Which accounts the service's own work is about.

Every row of the domain carries `account_id`, never the network: with two accounts of one network
the network identifies nothing (decision 10). This module is the one place the *service's own* code
asks which account that is: the worker, which goes account by account; the webhook, which finds the
account Meta names in each notification; and the first account of each mode, for the commands. The
operator's web interface takes its account from the URL (`ui/routes.py`).

An API call never comes here: its account is the caller's, resolved from the token or the OAuth
grant in `access/deps.py` (T02).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from jorgex_social.account.models import Connection


def account_of(session: Session, network: str) -> Connection | None:
    """The account of `network`, or None if nothing was ever written for it."""
    return session.scalars(
        select(Connection).where(Connection.network == network).order_by(Connection.id).limit(1)
    ).first()


def account_id_of(session: Session, network: str) -> str | None:
    """The id the queries filter by. None matches no row: nothing exists before the first write."""
    account = account_of(session, network)
    return account and account.id


def connected(account: Connection | None) -> bool:
    """Whether the account has been connected. A row without an external account id only owns
    what was written before the first connection (a draft post, the kill switch); the API still
    reports the account as missing, as it did when the row did not exist."""
    return account is not None and bool(account.external_account_id)


def current_account(session: Session, network: str) -> Connection:
    """The account of `network`, created if nothing has been connected yet: every row of the
    domain belongs to one, and a draft can be prepared before connecting. The new row carries no
    credential and no external id until `connect-instagram` fills it."""
    account = account_of(session, network)
    if account is None:
        account = Connection(network=network, external_account_id="", username="")
        session.add(account)
        session.flush()
    return account


def connected_accounts(session: Session) -> list[Connection]:
    """The accounts a credential can be bound to: the ones that have been connected, oldest
    first. A row created by a first write is not one of them yet."""
    return list(
        session.scalars(
            select(Connection).where(Connection.external_account_id != "").order_by(Connection.id)
        )
    )


def accounts_of(
    session: Session, network: str, *, connected_only: bool = False
) -> list[Connection]:
    """The connections of `network`, oldest first. `connected_only` leaves out the row a first
    write created before `connect-instagram` named its account: in simulation, where nothing has
    to be connected, the worker works on every row; in real mode only on the connected ones."""
    query = select(Connection).where(Connection.network == network).order_by(Connection.id)
    if connected_only:
        query = query.where(Connection.external_account_id != "")
    return list(session.scalars(query))


def connection_by_external_id(
    session: Session, network: str, external_id: str
) -> Connection | None:
    """The connection of `network` that the network itself calls `external_id`: the account a
    webhook notification says it is about (`entry[].id`)."""
    return session.scalar(
        select(Connection).where(
            Connection.network == network, Connection.external_account_id == external_id
        )
    )
