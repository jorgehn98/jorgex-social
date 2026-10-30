from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from jorgex_social.access.deps import SessionDep, require
from jorgex_social.access.tokens import Group, Level
from jorgex_social.replies import rules
from jorgex_social.replies.models import (
    Action,
    Channel,
    Conversation,
    Delivery,
    DeliveryStatus,
    Event,
    Reason,
    Rule,
)
from jorgex_social.replies.rules import RuleContent

router = APIRouter(prefix="/v1", tags=["replies"])

RulesRead = [Depends(require(Group.RULES, Level.READ))]
RulesWrite = [Depends(require(Group.RULES, Level.WRITE))]
EventsRead = [Depends(require(Group.EVENTS, Level.READ))]

Limit = Annotated[int, Query(ge=1, le=1000)]
Offset = Annotated[int, Query(ge=0)]
MediaRef = Annotated[str | None, Field(None, min_length=1, max_length=64)]


def _error(status_code: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code, detail={"code": code, "message": message, **extra})


def _network(request: Request) -> str:
    # Simulated rules and events never show in real mode, and the other way round.
    return request.app.state.network


Network = Annotated[str, Depends(_network)]


# Rules


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    channel: Channel
    media_ref: MediaRef = None
    priority: int = Field(100, ge=0, le=1_000_000)
    content: RuleContent


class RulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=100)
    media_ref: MediaRef = None
    priority: int | None = Field(None, ge=0, le=1_000_000)
    content: RuleContent | None = None


class RuleOut(BaseModel):
    id: str
    network: str
    name: str
    channel: str
    media_ref: str | None
    priority: int
    active: bool
    activated_at: datetime | None
    content: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


def _check(channel: Channel, media_ref: str | None, content: RuleContent) -> None:
    try:
        rules.check(channel, media_ref, content)
    except ValueError as exc:
        raise _error(422, "invalid_rule", str(exc)) from None


def _rule(session: Session, network: str, rule_id: str) -> Rule:
    rule = session.get(Rule, rule_id)
    if rule is None or rule.network != network:
        raise _error(404, "not_found", "No such rule.")
    return rule


def _not_archived(rule: Rule) -> None:
    if rule.archived_at is not None:
        raise _error(409, "invalid_state", "The rule is archived.")


@router.post(
    "/rules", status_code=status.HTTP_201_CREATED, response_model=RuleOut, dependencies=RulesWrite
)
def create_rule(body: RuleIn, request: Request, session: SessionDep, network: Network):
    """Create a rule, inactive. Activate it to make it answer; only what arrives after that
    is answered."""
    _check(body.channel, body.media_ref, body.content)
    now = request.app.state.clock.now()
    rule = Rule(
        network=network,
        name=body.name,
        channel=body.channel,
        media_ref=body.media_ref,
        priority=body.priority,
        active=False,
        content=body.content.model_dump(),
        created_at=now,
        updated_at=now,
    )
    session.add(rule)
    session.flush()
    return rule


@router.get("/rules", response_model=list[RuleOut], dependencies=RulesRead)
def list_rules(
    session: SessionDep,
    network: Network,
    channel: Channel | None = None,
    active: bool | None = None,
    archived: bool = False,
    limit: Limit = 100,
    offset: Offset = 0,
):
    """Rules in the order they are tried: lower priority first, then the oldest. Archived ones
    only with `archived=true`, and then only those."""
    query = select(Rule).where(Rule.network == network)
    query = query.where(Rule.archived_at.is_not(None) if archived else Rule.archived_at.is_(None))
    if channel is not None:
        query = query.where(Rule.channel == channel)
    if active is not None:
        query = query.where(Rule.active.is_(active))
    query = query.order_by(Rule.priority, Rule.created_at, Rule.id)
    return session.scalars(query.limit(limit).offset(offset)).all()


@router.get("/rules/{rule_id}", response_model=RuleOut, dependencies=RulesRead)
def get_rule(rule_id: str, session: SessionDep, network: Network):
    return _rule(session, network, rule_id)


@router.patch("/rules/{rule_id}", response_model=RuleOut, dependencies=RulesWrite)
def update_rule(
    rule_id: str, body: RulePatch, request: Request, session: SessionDep, network: Network
):
    """Edit a rule; only the fields given change, and `media_ref: null` makes it apply to every
    post. An active rule stays active and answers with the new content from now on."""
    rule = _rule(session, network, rule_id)
    _not_archived(rule)
    fields = body.model_fields_set
    for required in ("name", "priority", "content"):
        if required in fields and getattr(body, required) is None:
            raise _error(422, "invalid_value", f"{required} cannot be null.")
    media_ref = body.media_ref if "media_ref" in fields else rule.media_ref
    content = body.content if "content" in fields else RuleContent.model_validate(rule.content)
    _check(Channel(rule.channel), media_ref, content)
    if "name" in fields:
        rule.name = body.name
    if "priority" in fields:
        rule.priority = body.priority
    rule.media_ref = media_ref
    rule.content = content.model_dump()
    rule.updated_at = request.app.state.clock.now()
    return rule


@router.post("/rules/{rule_id}/activate", response_model=RuleOut, dependencies=RulesWrite)
def activate_rule(rule_id: str, request: Request, session: SessionDep, network: Network):
    """Start answering. Only comments and messages written from now on fire the rule."""
    rule = _rule(session, network, rule_id)
    _not_archived(rule)
    if not rule.active:
        now = request.app.state.clock.now()
        rule.active, rule.activated_at, rule.updated_at = True, now, now
    return rule


@router.post("/rules/{rule_id}/deactivate", response_model=RuleOut, dependencies=RulesWrite)
def deactivate_rule(rule_id: str, request: Request, session: SessionDep, network: Network):
    """Stop answering. Replies already queued are still sent."""
    rule = _rule(session, network, rule_id)
    if rule.active:
        rule.active, rule.updated_at = False, request.app.state.clock.now()
    return rule


@router.post("/rules/{rule_id}/archive", response_model=RuleOut, dependencies=RulesWrite)
def archive_rule(rule_id: str, request: Request, session: SessionDep, network: Network):
    """Deactivate and archive: the rule can no longer be edited or activated."""
    rule = _rule(session, network, rule_id)
    if rule.archived_at is None:
        now = request.app.state.clock.now()
        rule.active, rule.archived_at, rule.updated_at = False, now, now
    return rule


class RuleTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Channel
    text: str = Field(min_length=1, max_length=10_000)
    media_ref: MediaRef = None
    # Test this rule, active or not; without it, the active rules as the worker would.
    rule_id: str | None = None


class RuleTestOut(BaseModel):
    rule: RuleOut | None
    public_replies: list[str]
    private_replies: list[str]
    # Why the given rule did not match although the text might, e.g. a media_ref it needs.
    note: str | None = None


@router.post("/rules/test", response_model=RuleTestOut, dependencies=RulesRead)
def test_rules(body: RuleTestIn, session: SessionDep, network: Network):
    """Which rule a sample text would fire and what it could answer. Stores nothing and calls
    nothing on Instagram."""
    if body.rule_id is not None:
        candidates = [_rule(session, network, body.rule_id)]
        if candidates[0].channel != body.channel:
            raise _error(422, "invalid_value", "The rule is for another channel.")
    else:
        candidates = rules.active_rules(session, network, body.channel)
    rule = rules.first_match(candidates, body.text, media_ref=body.media_ref)
    content = rule.content if rule else {}
    note = None
    if body.rule_id is not None and rule is None:
        needed = candidates[0].media_ref
        if needed is not None and needed != body.media_ref:
            note = f"The rule only applies to comments on media {needed}; pass that media_ref."
    return RuleTestOut(
        rule=RuleOut.model_validate(rule, from_attributes=True) if rule else None,
        public_replies=content.get("public_replies", []),
        private_replies=content.get("private_replies", []),
        note=note,
    )


# Events, deliveries and conversations


class EventOut(BaseModel):
    id: str
    network: str
    type: str
    external_ref: str
    media_ref: str | None
    conversation_id: str | None
    occurred_at: datetime
    received_at: datetime
    rule_id: str | None


class DeliveryOut(BaseModel):
    id: str
    event_id: str
    rule_id: str
    action: str
    status: str
    reason: str | None
    text: str
    window_until_at_send: datetime | None
    result_ref: str | None
    error_code: int | None
    error_subcode: int | None
    created_at: datetime
    attempted_at: datetime | None
    finished_at: datetime | None


class ConversationOut(BaseModel):
    id: str
    network: str
    external_id: str
    last_user_message_at: datetime
    window_until: datetime
    paused_until: datetime | None
    paused_forever: bool
    purge_at: datetime


@router.get("/events", response_model=list[EventOut], dependencies=EventsRead)
def list_events(
    session: SessionDep,
    network: Network,
    type: Channel | None = None,
    rule_id: str | None = None,
    matched: bool | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
):
    """Comments and messages seen, newest first: identifiers and the rule they fired, never
    their text or author."""
    query = select(Event).where(Event.network == network)
    if type is not None:
        query = query.where(Event.type == type)
    if rule_id is not None:
        query = query.where(Event.rule_id == rule_id)
    if matched is not None:
        query = query.where(Event.rule_id.is_not(None) if matched else Event.rule_id.is_(None))
    query = query.order_by(Event.received_at.desc(), Event.id.desc())
    return session.scalars(query.limit(limit).offset(offset)).all()


@router.get("/deliveries", response_model=list[DeliveryOut], dependencies=EventsRead)
def list_deliveries(
    session: SessionDep,
    network: Network,
    status: DeliveryStatus | None = None,
    rule_id: str | None = None,
    reason: Reason | None = None,
    action: Action | None = None,
    event_id: str | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
):
    """Replies, newest first, with what was sent or why not."""
    query = select(Delivery).join(Event).where(Event.network == network)
    for column, value in (
        (Delivery.status, status),
        (Delivery.rule_id, rule_id),
        (Delivery.reason, reason),
        (Delivery.action, action),
        (Delivery.event_id, event_id),
    ):
        if value is not None:
            query = query.where(column == value)
    query = query.order_by(Delivery.created_at.desc(), Delivery.id.desc())
    return session.scalars(query.limit(limit).offset(offset)).all()


@router.get("/conversations", response_model=list[ConversationOut], dependencies=EventsRead)
def list_conversations(
    request: Request,
    session: SessionDep,
    network: Network,
    paused: bool | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
):
    """People who wrote by direct message, most recent first, with their window and pause."""
    query = select(Conversation).where(Conversation.network == network)
    if paused is not None:
        now = request.app.state.clock.now()
        is_paused = Conversation.paused_forever.is_(True) | (Conversation.paused_until > now)
        query = query.where(is_paused if paused else ~is_paused)
    query = query.order_by(Conversation.last_user_message_at.desc(), Conversation.id.desc())
    return session.scalars(query.limit(limit).offset(offset)).all()


class PauseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    until: AwareDatetime | None = None
    forever: bool = False


def _conversation(session: Session, network: str, conversation_id: str) -> Conversation:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.network != network:
        raise _error(404, "not_found", "No such conversation.")
    return conversation


@router.post(
    "/conversations/{conversation_id}/pause",
    response_model=ConversationOut,
    dependencies=RulesWrite,
)
def pause_conversation(
    conversation_id: str, body: PauseIn, request: Request, session: SessionDep, network: Network
):
    """Stop automatic messages to this person, until a time or for good, for example while a
    human answers. Messages already queued are skipped as `paused`."""
    if body.forever == (body.until is not None):
        raise _error(422, "invalid_value", "Give either until or forever=true.")
    now = request.app.state.clock.now()
    if body.until is not None and body.until <= now:
        raise _error(422, "in_the_past", "until is in the past.")
    conversation = _conversation(session, network, conversation_id)
    conversation.paused_forever = body.forever
    conversation.paused_until = body.until
    conversation.updated_at = now
    return conversation


@router.post(
    "/conversations/{conversation_id}/resume",
    response_model=ConversationOut,
    dependencies=RulesWrite,
)
def resume_conversation(
    conversation_id: str, request: Request, session: SessionDep, network: Network
):
    """Lift a pause. Messages skipped while paused are not sent afterwards."""
    conversation = _conversation(session, network, conversation_id)
    conversation.paused_forever = False
    conversation.paused_until = None
    conversation.updated_at = request.app.state.clock.now()
    return conversation
