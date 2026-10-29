import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jorgex_social.account import audit
from jorgex_social.account.models import ChannelIncident, IncidentType

log = logging.getLogger(__name__)


def sync(
    session: Session,
    network: str,
    type_: IncidentType,
    active: bool,
    now: datetime,
    detail: dict[str, Any] | None = None,
) -> None:
    """Opens the incident when its cause appears and closes it when it is gone. While the cause
    lasts, the open incident only gets its detail refreshed: it is never opened twice."""
    current = session.scalar(
        select(ChannelIncident).where(
            ChannelIncident.network == network,
            ChannelIncident.type == type_,
            ChannelIncident.closed_at.is_(None),
        )
    )
    if active and current is None:
        current = ChannelIncident(network=network, type=type_, opened_at=now, detail=detail)
        session.add(current)
        session.flush()
        audit.record(session, now, "incident.opened", object_type="incident", object_id=current.id)
        log.warning("incident opened", extra={"type": str(type_), "network": network})
    elif active:
        current.detail = detail
    elif current is not None:
        current.closed_at = now
        audit.record(session, now, "incident.closed", object_type="incident", object_id=current.id)
        log.info("incident closed", extra={"type": str(type_), "network": network})
