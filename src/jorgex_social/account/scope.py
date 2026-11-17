"""Which account the service is working on.

Every row of the domain carries `account_id`, never the network: with two accounts of one network
the network identifies nothing (decision 10). This module is the one place that says which account
that is, so the queries only ever ask it.

Today one installation serves one account per mode (decision 07): the real one, or the simulated
one. T02 replaces the account of the mode with the account of the caller's token, and only what
these functions read changes.
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
