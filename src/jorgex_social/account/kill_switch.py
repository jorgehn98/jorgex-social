"""The kill switch: while on, nothing is written to the network (no containers, no publishing,
no replies). Reads such as snapshots and connection checks go on. The operator sets it, and it
turns on by itself when Instagram reports the account as restricted."""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from jorgex_social.account import audit, incidents
from jorgex_social.account.models import IncidentType, Setting
from jorgex_social.instagram.errors import AccountRestricted

log = logging.getLogger(__name__)

KEY = "kill_switch"


def is_on(session: Session) -> bool:
    setting = session.get(Setting, KEY)
    return bool(setting and setting.value)


def set_state(session: Session, enabled: bool, now: datetime) -> Setting:
    current = session.get(Setting, KEY)
    if current is None:
        current = Setting(key=KEY, value=enabled, updated_at=now)
        session.add(current)
    elif current.value != enabled:
        current.value = enabled
        current.updated_at = now
    return current


def trip(session: Session, network: str, error: AccountRestricted, now: datetime) -> None:
    """Stops all writes because Instagram restricted the account, in the caller's transaction.
    Only the operator turning the switch off closes the incident (account/api.py)."""
    detail = {"code": error.code, "subcode": error.subcode}
    if not is_on(session):
        set_state(session, True, now)
        audit.record(session, now, "kill_switch.on", detail=detail)
        log.error("account restricted; kill switch on", extra=detail)
    incidents.sync(session, network, IncidentType.ACCOUNT_RESTRICTED, True, now, detail)
