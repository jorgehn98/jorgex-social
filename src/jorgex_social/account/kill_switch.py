"""The kill switch: while on for an account, nothing is written to its network (no containers, no
publishing, no replies). Reads such as snapshots and connection checks go on. The operator sets it,
and it turns on by itself when the network reports the account as restricted. Turning it on for
one account leaves the others alone (decision 10)."""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from jorgex_social.account import audit, incidents
from jorgex_social.account.models import Connection, IncidentType
from jorgex_social.instagram.errors import AccountRestricted

log = logging.getLogger(__name__)


def is_on(session: Session, account_id: str) -> bool:
    account = session.get(Connection, account_id)
    return bool(account and account.kill_switch)


def set_state(session: Session, account: Connection, enabled: bool, now: datetime) -> Connection:
    # The first state written is a moment too, even when the switch stays off: the API reports
    # when it last changed.
    if account.kill_switch != enabled or account.kill_switch_updated_at is None:
        account.kill_switch = enabled
        account.kill_switch_updated_at = now
    return account


def trip(session: Session, account_id: str, error: AccountRestricted, now: datetime) -> None:
    """Stops all writes for the account because the network restricted it, in the caller's
    transaction. Only turning the switch off closes the incident (account/api.py)."""
    account = session.get_one(Connection, account_id)
    detail = {"code": error.code, "subcode": error.subcode}
    if not account.kill_switch:
        set_state(session, account, True, now)
        audit.record(session, now, "kill_switch.on", account_id=account_id, detail=detail)
        log.error("account restricted; kill switch on", extra=detail)
    incidents.sync(session, account_id, IncidentType.ACCOUNT_RESTRICTED, True, now, detail)
