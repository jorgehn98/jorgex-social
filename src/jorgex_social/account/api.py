from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import false, func, select
from sqlalchemy.orm import Session

from jorgex_social.access.deps import AccountDep, AccountIdDep, ReadSessionDep, SessionDep, require
from jorgex_social.access.tokens import Group, Level
from jorgex_social.account import appearance, audit, incidents, kill_switch, scope
from jorgex_social.account.connection import status_at
from jorgex_social.account.models import (
    AuditEntry,
    ChannelIncident,
    Connection,
    ConnectionStatus,
    IncidentType,
)
from jorgex_social.instagram.fake import FakeInstagram

router = APIRouter(prefix="/v1/account", tags=["account"])
appearance_router = APIRouter(prefix="/v1/appearance", tags=["account"])

AccountRead = [Depends(require(Group.ACCOUNT, Level.READ))]
AccountWrite = [Depends(require(Group.ACCOUNT, Level.WRITE))]

Limit = Annotated[int, Query(ge=1, le=1000)]
Offset = Annotated[int, Query(ge=0)]


class ConnectedAccount(BaseModel):
    id: str
    username: str
    account_type: str | None


class WebhookOut(BaseModel):
    configured: bool
    # Last notification with a valid signature; null if none has arrived yet.
    last_notification_at: datetime | None


class AccountOut(BaseModel):
    """The connection as the service sees it. The credential itself is never part of it."""

    network: str
    # True: publishing, replies and the kill switch act on a fake account, never on Instagram.
    simulation: bool
    status: ConnectionStatus
    account: ConnectedAccount | None
    credential_expires_at: datetime | None
    checked_at: datetime | None
    limits: dict[str, Any] | None
    kill_switch: bool
    open_incidents: int
    webhook: WebhookOut


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


class IconOut(BaseModel):
    type: str
    bytes: int


class AppearanceSaved(BaseModel):
    name: str
    # The part of the name shown in the accent colour; empty for none.
    name_highlight: str
    # Null: the default icon.
    icon: IconOut | None


class AppearanceOut(AppearanceSaved):
    css: str
    # What the CSS can override, with the default values.
    variables: dict[str, str]
    # Custom properties in the CSS this interface does not read: likely typos. They block nothing.
    warnings: list[str]


class AppearanceIn(BaseModel):
    """Only what is sent changes. Null or absent leaves a field as it is. There is no `css`, on
    purpose: see `set_appearance`."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def refuse_css(cls, data: Any) -> Any:
        # Before extra="forbid", whose message would not tell an agent where CSS is edited.
        if isinstance(data, dict) and "css" in data:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "invalid_appearance",
                    "message": "CSS can only be edited by the operator, in Settings.",
                },
            )
        return data

    name: str | None = None
    # An empty string removes it. A new name the current highlight does not occur in is refused
    # unless this is sent too.
    name_highlight: str | None = None
    icon_base64: str | None = None
    clear_icon: bool = False
    # Back to the default name and icon; the CSS stays. Alone: not with the fields above.
    reset: bool = False


@router.get("", response_model=AccountOut, dependencies=AccountRead)
def get_account(request: Request, session: SessionDep, account: AccountDep):
    """The account of this token, as the service sees it. The credential itself is never part of
    it, and no other account appears in any field."""
    return account_out(request, session, account, installation=False)


def account_out(
    request: Request, session: Session, account: Connection | None, *, installation: bool
) -> AccountOut:
    """The account as the API reports it. `account` is None when nothing exists for the mode yet:
    the web interface, which still resolves the one account of the mode until T04.

    `installation` adds the installation's own incidents to the count (the operator's sign-ins,
    which belong to no account): the web interface's view, never a token's."""
    now = request.app.state.clock.now()
    network = account.network if account else request.app.state.network
    of_account = ChannelIncident.account_id == account.id if account else false()
    if installation:
        of_account = of_account | ChannelIncident.account_id.is_(None)
    open_incidents = session.scalar(
        select(func.count()).where(of_account, ChannelIncident.closed_at.is_(None))
    )
    common = {
        "simulation": request.app.state.settings.simulation,
        "kill_switch": bool(account and account.kill_switch),
        "open_incidents": open_incidents,
        "webhook": WebhookOut(
            configured=request.app.state.settings.webhook_enabled,
            last_notification_at=account and account.webhook_notified_at,
        ),
    }
    if not scope.connected(account):
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
        status=status_at(account, now),
        account=ConnectedAccount(
            id=account.external_account_id,
            username=account.username,
            account_type=account.account_type,
        ),
        credential_expires_at=account.credential_expires_at,
        checked_at=account.checked_at,
        limits=account.limits,
        **common,
    )


def _incident_out(incident: ChannelIncident, network: str) -> IncidentOut:
    """The network comes from the connection, never from the row; an incident with no account (the
    operator's) is reported in the mode that shows it."""
    return IncidentOut(
        id=incident.id,
        network=network,
        type=incident.type,
        opened_at=incident.opened_at,
        closed_at=incident.closed_at,
        acknowledged_at=incident.acknowledged_at,
        detail=incident.detail,
    )


def incidents_page(
    session: Session,
    account_id: str | None,
    network: str,
    *,
    installation: bool,
    open: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[IncidentOut]:
    """Newest first. `installation` adds the installation's own incidents (the operator's sign-in
    attempts, which belong to no account): the web interface's view, never a token's.

    `account_id` None means nothing exists for the mode yet, so there is nothing of an account to
    list, and never every row without one."""
    of_account = ChannelIncident.account_id == account_id if account_id is not None else false()
    if installation:
        of_account = of_account | ChannelIncident.account_id.is_(None)
    query = select(ChannelIncident).where(of_account)
    if open is not None:
        closed = ChannelIncident.closed_at
        query = query.where(closed.is_(None) if open else closed.is_not(None))
    query = query.order_by(ChannelIncident.opened_at.desc(), ChannelIncident.id.desc())
    found = session.scalars(query.limit(limit).offset(offset)).all()
    return [_incident_out(incident, network) for incident in found]


@router.get("/incidents", response_model=list[IncidentOut], dependencies=AccountRead)
def list_incidents(
    session: SessionDep,
    account: AccountDep,
    open: bool | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
):
    """Newest first. `open=true` lists the ones whose cause is still there.

    Only this account's, and never the installation's own: `sign_in_password_accepted` means
    sign-ins with the right password kept failing on the code, so the password may have leaked,
    and it belongs to the operator, who sees it in the web interface. If it was not them, they
    change the password (`set-password`), which is what closes it; signing in does not."""
    return incidents_page(
        session,
        account.id,
        account.network,
        installation=False,
        open=open,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/incidents/{incident_id}/acknowledge", response_model=IncidentOut, dependencies=AccountWrite
)
def acknowledge_incident(
    incident_id: str, request: Request, session: SessionDep, account: AccountDep
):
    """Marks the incident as seen. It stays open until its cause is gone. Another account's
    incident, or the installation's own, is `not_found`: this token has no such incident."""
    incident = session.get(ChannelIncident, incident_id)
    if incident is None or incident.account_id != account.id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "No such incident."},
        )
    incident.acknowledged_at = incident.acknowledged_at or request.app.state.clock.now()
    return _incident_out(incident, account.network)


@router.put("/kill-switch", response_model=KillSwitchOut, dependencies=AccountWrite)
def set_kill_switch(body: KillSwitchIn, request: Request, session: SessionDep, account: AccountDep):
    """On: the worker stops writing to the network for this account (no containers, no publishing,
    no replies). Scheduled posts stay scheduled; those past the grace window when it is turned off
    fail as `missed_schedule` and can be rescheduled. Replies due while it is on are skipped.

    It acts on this token's account alone: another account's switch cannot be read or set from
    here, and turning it off leaves every other account as it was.

    It also turns on by itself when Instagram reports the account as restricted, with an
    `account_restricted` incident. Only turning it off here closes that incident; if Instagram
    refuses again, both come back."""
    audit.set_detail(request, {"enabled": body.enabled})
    now = request.app.state.clock.now()
    kill_switch.set_state(session, account, body.enabled, now)
    if not body.enabled:
        incidents.sync(session, account.id, IncidentType.ACCOUNT_RESTRICTED, False, now)
    return KillSwitchOut(enabled=account.kill_switch, updated_at=account.kill_switch_updated_at)


@router.get("/audit", response_model=list[AuditEntryOut], dependencies=AccountRead)
def list_audit(
    session: SessionDep,
    account: AccountIdDep,
    object_type: str | None = None,
    object_id: str | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
):
    """Newest first. This account's entries only: never another account's, and never the
    installation's own (the operator's sign-ins and Settings), which this token cannot see."""
    query = select(AuditEntry).where(AuditEntry.account_id == account)
    if object_type is not None:
        query = query.where(AuditEntry.object_type == object_type)
    if object_id is not None:
        query = query.where(AuditEntry.object_id == object_id)
    # Ids are UUIDv7, so they break ties between entries written in the same instant in order.
    query = query.order_by(AuditEntry.occurred_at.desc(), AuditEntry.id.desc())
    return session.scalars(query.limit(limit).offset(offset)).all()


def _appearance_saved(session: Session) -> AppearanceSaved:
    name = appearance.name_of(session)
    icon = appearance.icon_of(session)
    return AppearanceSaved(
        name=name.text,
        name_highlight=name.highlight,
        icon=icon and IconOut(type=icon.type, bytes=len(icon.data)),
    )


def _appearance_out(session: Session) -> AppearanceOut:
    css = appearance.css_of(session)
    return AppearanceOut(
        **_appearance_saved(session).model_dump(),
        css=css,
        variables=appearance.VARIABLES,
        warnings=appearance.warnings(css),
    )


@appearance_router.get("", response_model=AppearanceOut, dependencies=AccountRead)
def get_appearance(session: ReadSessionDep):
    """The product name, the CSS loaded after the default stylesheet, the icon's type and size,
    the variables the CSS can override, and `warnings`: custom properties in the CSS this interface
    does not read, likely typos. The icon itself is at `GET /ui/icon`."""
    return _appearance_out(session)


# A deliberate exception to the rule that everything the platform does reaches the API and the MCP:
# CSS cannot be written here, whatever the token's permissions. Valid CSS can change what a page
# shows, for example hide an open incident, so it would let an agent hide things from the operator
# who oversees it. Do not add it back. The operator edits it in Settings; reading it is harmless.
@appearance_router.put("", response_model=AppearanceSaved, dependencies=AccountWrite)
def set_appearance(body: AppearanceIn, request: Request, session: SessionDep):
    """Name: 1 to 40 characters after stripping. Name highlight: a part of the name, shown in the
    accent colour (first occurrence, case-sensitive); changing the name so that the highlight no
    longer occurs is refused unless the request also changes or empties `name_highlight`. Icon:
    PNG, ICO or SVG up to 256 KB, base64, its type decided by its content. `reset` puts the name,
    highlight and icon back to their defaults and leaves the CSS untouched. Anything else is 422
    `invalid_appearance`, and nothing changes.

    CSS cannot be sent here: the operator edits it in Settings."""
    try:
        changed = appearance.update(
            session,
            request.app.state.clock.now(),
            name=body.name,
            name_highlight=body.name_highlight,
            icon=None if body.icon_base64 is None else appearance.decode_icon(body.icon_base64),
            clear_icon=body.clear_icon,
            reset=body.reset,
        )
    except appearance.InvalidAppearance as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_appearance", "message": str(error)},
        ) from None
    audit.set_detail(request, {"changed": changed})
    return _appearance_saved(session)
