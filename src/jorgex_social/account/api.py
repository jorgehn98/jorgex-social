from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select

from jorgex_social.access.deps import SessionDep, require
from jorgex_social.access.tokens import Group, Level
from jorgex_social.account import audit, kill_switch
from jorgex_social.account.connection import status_at
from jorgex_social.account.credential import connection_of
from jorgex_social.account.models import AuditEntry, ChannelIncident, ConnectionStatus
from jorgex_social.instagram.fake import FakeInstagram

router = APIRouter(prefix="/v1/account", tags=["account"])

AccountRead = [Depends(require(Group.ACCOUNT, Level.READ))]
AccountWrite = [Depends(require(Group.ACCOUNT, Level.WRITE))]

Limit = Annotated[int, Query(ge=1, le=1000)]
Offset = Annotated[int, Query(ge=0)]


class ConnectedAccount(BaseModel):
    id: str
    username: str
    account_type: str | None


class AccountOut(BaseModel):
    """The connection as the service sees it. The credential itself is never part of it."""

    network: str
    status: ConnectionStatus
    account: ConnectedAccount | None
    credential_expires_at: datetime | None
    checked_at: datetime | None
    limits: dict[str, Any] | None
    kill_switch: bool
    open_incidents: int


class IncidentOut(BaseModel):
    id: str
    network: str
    type: str
    opened_at: datetime
    closed_at: datetime | None
    acknowledged_at: datetime | None
    detail: dict[str, Any] | None


class KillSwitchIn(BaseModel):
    enabled: bool


class KillSwitchOut(BaseModel):
    enabled: bool
    updated_at: datetime


class AuditEntryOut(BaseModel):
    id: str
    occurred_at: datetime
    actor_type: str
    actor_ref: str | None
    action: str
    object_type: str | None
    object_id: str | None
    outcome: str
    detail: dict[str, Any] | None


@router.get("", response_model=AccountOut, dependencies=AccountRead)
def get_account(request: Request, session: SessionDep):
    network = request.app.state.network
    now = request.app.state.clock.now()
    connection = connection_of(session, network)
    open_incidents = session.scalar(
        select(func.count()).where(
            ChannelIncident.network == network, ChannelIncident.closed_at.is_(None)
        )
    )
    common = {"kill_switch": kill_switch.is_on(session), "open_incidents": open_incidents}
    if connection is None:
        # The fake needs no credential, so simulation works before the worker's first check.
        simulated = network == FakeInstagram.network
        return AccountOut(
            network=network,
            status=ConnectionStatus.OK if simulated else ConnectionStatus.MISSING,
            account=None,
            credential_expires_at=None,
            checked_at=None,
            limits=None,
            **common,
        )
    return AccountOut(
        network=network,
        status=status_at(connection, now),
        account=ConnectedAccount(
            id=connection.external_account_id,
            username=connection.username,
            account_type=connection.account_type,
        ),
        credential_expires_at=connection.credential_expires_at,
        checked_at=connection.checked_at,
        limits=connection.limits,
        **common,
    )


@router.get("/incidents", response_model=list[IncidentOut], dependencies=AccountRead)
def list_incidents(
    request: Request,
    session: SessionDep,
    open: bool | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
):
    """Newest first. `open=true` lists the ones whose cause is still there."""
    query = select(ChannelIncident).where(ChannelIncident.network == request.app.state.network)
    if open is not None:
        closed = ChannelIncident.closed_at
        query = query.where(closed.is_(None) if open else closed.is_not(None))
    query = query.order_by(ChannelIncident.opened_at.desc(), ChannelIncident.id.desc())
    return session.scalars(query.limit(limit).offset(offset)).all()


@router.post(
    "/incidents/{incident_id}/acknowledge", response_model=IncidentOut, dependencies=AccountWrite
)
def acknowledge_incident(incident_id: str, request: Request, session: SessionDep):
    """Marks the incident as seen. It stays open until its cause is gone."""
    incident = session.get(ChannelIncident, incident_id)
    if incident is None or incident.network != request.app.state.network:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "No such incident."},
        )
    incident.acknowledged_at = incident.acknowledged_at or request.app.state.clock.now()
    return incident


@router.put("/kill-switch", response_model=KillSwitchOut, dependencies=AccountWrite)
def set_kill_switch(body: KillSwitchIn, request: Request, session: SessionDep):
    """On: the worker stops writing to the network (no containers, no publishing). Scheduled
    posts stay scheduled; those past the grace window when it is turned off fail as
    `missed_schedule` and can be rescheduled."""
    audit.set_detail(request, {"enabled": body.enabled})
    setting = kill_switch.set_state(session, body.enabled, request.app.state.clock.now())
    return KillSwitchOut(enabled=setting.value, updated_at=setting.updated_at)


@router.get("/audit", response_model=list[AuditEntryOut], dependencies=AccountRead)
def list_audit(
    session: SessionDep,
    object_type: str | None = None,
    object_id: str | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
):
    """Newest first."""
    query = select(AuditEntry)
    if object_type is not None:
        query = query.where(AuditEntry.object_type == object_type)
    if object_id is not None:
        query = query.where(AuditEntry.object_id == object_id)
    # Ids are UUIDv7, so they break ties between entries written in the same instant in order.
    query = query.order_by(AuditEntry.occurred_at.desc(), AuditEntry.id.desc())
    return session.scalars(query.limit(limit).offset(offset)).all()
