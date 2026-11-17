import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jorgex_social.account import audit
from jorgex_social.account.models import ChannelIncident, IncidentType

log = logging.getLogger(__name__)


def _sync(
    session: Session,
    account_id: str | None,
    type_: IncidentType,
    active: bool,
    now: datetime,
    detail: dict[str, Any] | None = None,
) -> None:
    of_the_account = (
        ChannelIncident.account_id.is_(None)
        if account_id is None
        else ChannelIncident.account_id == account_id
    )
    current = session.scalar(
        select(ChannelIncident).where(
            of_the_account,
            ChannelIncident.type == type_,
            ChannelIncident.closed_at.is_(None),
        )
    )
    if active and current is None:
        current = ChannelIncident(account_id=account_id, type=type_, opened_at=now, detail=detail)
        session.add(current)
        session.flush()
        audit.record(
            session,
            now,
            "incident.opened",
            account_id=account_id,
            object_type="incident",
            object_id=current.id,
        )
        log.warning("incident opened", extra={"type": str(type_), "account_id": account_id})
    elif active:
        current.detail = detail
    elif current is not None:
        current.closed_at = now
        audit.record(
            session,
            now,
            "incident.closed",
            account_id=account_id,
            object_type="incident",
            object_id=current.id,
        )
        log.info("incident closed", extra={"type": str(type_), "account_id": account_id})


def sync(
    session: Session,
    account_id: str,
    type_: IncidentType,
    active: bool,
    now: datetime,
    detail: dict[str, Any] | None = None,
) -> None:
    """Opens the incident when its cause appears and closes it when it is gone. While the cause
    lasts, the open incident only gets its detail refreshed: it is never opened twice.

    An incident belongs to an account: its own, and never another's. The installation's own
    (the operator's sign-ins) go through `sync_operator`, so a caller without an account cannot
    file a domain incident as the operator's by omission."""
    _sync(session, account_id, type_, active, now, detail)


def sync_operator(
    session: Session,
    type_: IncidentType,
    active: bool,
    now: datetime,
    detail: dict[str, Any] | None = None,
) -> None:
    """The installation's own incidents, which are no account's. Only the web interface and the
    sign-in flow see them: a token reads and acknowledges its account's alone."""
    _sync(session, None, type_, active, now, detail)
