"""Meta's webhook: `GET /webhook` answers the subscription check, `POST /webhook` receives
notifications of new comments and direct messages.

A notification only feeds the intake that polling uses (replies/intake.py): the same filters,
deduplication and rules, and the worker sends the replies. Polling stays on as the reconciler,
since Meta may retry, reorder or drop notifications.

The body carries third-party text and usernames. It is never logged or stored, whatever the
signature: what is logged is counts and outcomes only.

Payload shapes, from Meta's "Instagram webhook notification examples" (Business Login for
Instagram) and "Set up Instagram Webhooks", read on 2026-10-30. What those pages leave open is
marked UNVERIFIED below; the parser ignores anything it does not recognize instead of failing.
"""

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from jorgex_social.account import audit, incidents
from jorgex_social.account.credential import connection_of
from jorgex_social.account.models import IncidentType
from jorgex_social.instagram.types import Comment, Message
from jorgex_social.replies.intake import Intake
from jorgex_social.replies.models import (
    Action,
    Conversation,
    Delivery,
    DeliveryStatus,
    Event,
    Source,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["webhook"], include_in_schema=False)

# Meta batches at most 1,000 updates per request; ours are a handful of small objects each.
MAX_BODY_BYTES = 1024 * 1024
SIGNATURE_HEADER = "x-hub-signature-256"
# An unknown echo this soon after we sent a message to the same person is taken as that send:
# a safety net in case the echo's mid is not the message_id the send returned (unverified).
RECENT_SEND = timedelta(minutes=2)
# Above this a Unix time is in milliseconds. Meta's examples give `entry.time` in seconds for
# Graph notifications and in milliseconds for messaging ones (1569262486134).
_MILLISECONDS_FROM = 10**11


@dataclass(frozen=True)
class Echo:
    """A direct message the account sent, as Meta reports it back (`is_echo`)."""

    message_id: str
    recipient_id: str


@dataclass
class Notification:
    comments: list[tuple[str, Comment]] = field(default_factory=list)  # (media id, comment)
    messages: list[Message] = field(default_factory=list)
    echoes: list[Echo] = field(default_factory=list)
    ignored: int = 0  # entries or changes not used: other fields, other accounts, odd shapes
    matched: int = 0  # entries for our account, useful or not
    other_accounts: set[str] = field(default_factory=set)  # entry ids that are not ours


def _str_id(value: Any) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
        return str(value)
    return None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    seconds = value / 1000 if value >= _MILLISECONDS_FROM else value
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except OverflowError, OSError, ValueError:
        return None


def _comment(value: Any, sent_at: datetime) -> tuple[str, Comment] | None:
    """A top-level comment on the account's media, or None.

    Instagram Login names the comment `id`; Meta's Unified Login example names it `comment_id`.
    Both are read. UNVERIFIED: whether Instagram Login sends `parent_id` for a reply to a
    comment, as the Unified Login example documents; replies are skipped when it is there, as
    polling only reads top-level comments. The payload has no comment time, so the time Meta
    sent the notification stands in for it."""
    if not isinstance(value, dict) or value.get("parent_id") is not None:
        return None
    comment_id = _str_id(value.get("id")) or _str_id(value.get("comment_id"))
    author = value.get("from")
    media = value.get("media")
    media_id = _str_id(media.get("id")) if isinstance(media, dict) else None
    if comment_id is None or media_id is None:
        return None
    text = value.get("text")
    return media_id, Comment(
        id=comment_id,
        # No author: the intake drops it, as it cannot be told apart from the account's own.
        author_id=_str_id(author.get("id")) if isinstance(author, dict) else None,
        timestamp=sent_at,
        text=text if isinstance(text, str) else "",
    )


def _messaging(item: Any, notification: Notification) -> None:
    """One `messaging` item: a message from someone, an echo of the account's own, or anything
    else (reactions, seen, postbacks, edits), which is ignored."""
    if not isinstance(item, dict) or not isinstance(item.get("message"), dict):
        notification.ignored += 1
        return
    message = item["message"]
    sender, recipient = item.get("sender"), item.get("recipient")
    sender_id = _str_id(sender.get("id")) if isinstance(sender, dict) else None
    recipient_id = _str_id(recipient.get("id")) if isinstance(recipient, dict) else None
    mid = _str_id(message.get("mid"))
    # `timestamp` is documented as "the time the webhook was triggered" (milliseconds in the
    # examples); UNVERIFIED that it equals the message's `created_time` that polling reads.
    created_at = _time(item.get("timestamp"))
    # Deleted messages and the test message sent to the account itself are not new activity.
    if mid is None or created_at is None or message.get("is_deleted") or message.get("is_self"):
        notification.ignored += 1
    elif message.get("is_echo"):
        if recipient_id is None:
            notification.ignored += 1
        else:
            notification.echoes.append(Echo(mid, recipient_id))
    else:
        text = message.get("text")
        notification.messages.append(
            Message(mid, sender_id, created_at, text if isinstance(text, str) else "")
        )


def parse(payload: Any, own: str, now: datetime) -> Notification:
    """What a notification says about the account `own`. Never raises on an unexpected shape."""
    notification = Notification()
    entries = payload.get("entry") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("object") != "instagram":
        notification.ignored += 1
        return notification
    for entry in entries if isinstance(entries, list) else []:
        # entry.id is "the Instagram professional account ID of your app user": the `user_id`
        # the connection stores. One installation serves one account; anything else is dropped.
        entry_id = _str_id(entry.get("id")) if isinstance(entry, dict) else None
        if entry_id != own:
            notification.ignored += 1
            if entry_id is not None:
                notification.other_accounts.add(entry_id)
            continue
        notification.matched += 1
        sent_at = min(_time(entry.get("time")) or now, now)
        # Meta's Instagram Login example puts `field` and `value` on the entry itself; its
        # Unified Login examples, and the generic Graph format, wrap them in `changes`. Both read.
        changes = entry.get("changes")
        changes = changes if isinstance(changes, list) else []
        if "field" in entry:
            changes = [*changes, {"field": entry.get("field"), "value": entry.get("value")}]
        for change in changes:
            found = (
                _comment(change.get("value"), sent_at)
                if isinstance(change, dict) and change.get("field") == "comments"
                else None
            )
            if found is None:
                notification.ignored += 1
            else:
                notification.comments.append(found)
        messaging = entry.get("messaging")
        for item in messaging if isinstance(messaging, list) else []:
            _messaging(item, notification)
    return notification


def signature_valid(secret: str, body: bytes, header: str | None) -> bool:
    if not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Starlette decodes headers as latin-1, so this encoding gives back the bytes received.
    return hmac.compare_digest(expected.encode(), header.encode("latin-1"))


def _sending(session: Session, network: str, conversation: Conversation) -> bool:
    """Meta can echo a reply before the worker has stored its id, so an unknown echo may be a
    send still in flight: a message to this conversation, or any private reply (its comment's
    author is not stored, so it cannot be tied to a conversation)."""
    return (
        session.scalar(
            select(Delivery.id)
            .join(Event)
            .where(
                Event.network == network,
                Delivery.status == DeliveryStatus.PENDING,
                Delivery.attempted_at.is_not(None),
                or_(
                    and_(
                        Delivery.action == Action.MESSAGE,
                        Event.conversation_id == conversation.id,
                    ),
                    Delivery.action == Action.PRIVATE_REPLY,
                ),
            )
            .limit(1)
        )
        is not None
    )


def _sent_recently(session: Session, conversation: Conversation, now: datetime) -> bool:
    return (
        session.scalar(
            select(Delivery.id)
            .join(Event)
            .where(
                Event.conversation_id == conversation.id,
                Delivery.status == DeliveryStatus.SENT,
                Delivery.finished_at > now - RECENT_SEND,
            )
            .limit(1)
        )
        is not None
    )


def _pause_for_echoes(
    session: Session, network: str, echoes: list[Echo], now: datetime, pause: timedelta
) -> int:
    """An echo that is none of our replies means someone answered by hand: the conversation
    with that person pauses. Returns how many conversations were paused."""
    if not echoes:
        return 0
    ids = [e.message_id for e in echoes]
    ours = set(session.scalars(select(Delivery.result_ref).where(Delivery.result_ref.in_(ids))))
    paused = 0
    for echo in echoes:
        if echo.message_id in ours:
            continue
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.network == network, Conversation.external_id == echo.recipient_id
            )
        )
        until = now + pause
        if (
            conversation is None  # nobody to pause: the person has not written within retention
            or conversation.paused_forever
            or (conversation.paused_until is not None and conversation.paused_until >= until)
            or _sending(session, network, conversation)
            or _sent_recently(session, conversation, now)
        ):
            continue
        conversation.paused_until = until
        conversation.updated_at = now
        audit.record(
            session,
            now,
            "conversation.paused",
            object_type="conversation",
            object_id=conversation.id,
            detail={"cause": "human_reply", "until": until.isoformat()},
        )
        paused += 1
    return paused


def _process(request: Request, payload: Any) -> None:
    state = request.app.state
    network = state.network
    now = state.clock.now()
    with state.sessionmaker() as session, session.begin():
        connection = connection_of(session, network)
        if connection is None:
            log.warning("webhook notification before any account is connected; ignored")
            return
        own = connection.external_account_id
        notification = parse(payload, own, now)
        if notification.other_accounts:
            # Ids only: an account id, never anything from the body.
            log.warning(
                "webhook notification for another account; ignored",
                extra={"account_ids": sorted(notification.other_accounts), "expected": own},
            )
        # Only a notification about our account shows that the webhook works for it.
        if notification.matched:
            connection.webhook_notified_at = now
            incidents.sync(session, network, IncidentType.WEBHOOK_SILENT, False, now)
        intake: Intake = state.intake
        intake.comments(session, own, notification.comments, now, Source.WEBHOOK)
        intake.messages(session, own, notification.messages, now, Source.WEBHOOK)
        paused = _pause_for_echoes(session, network, notification.echoes, now, state.human_pause)
    log.info(
        "webhook notification",
        extra={
            "comments": len(notification.comments),
            "messages": len(notification.messages),
            "echoes": len(notification.echoes),
            "paused": paused,
            "ignored": notification.ignored,
        },
    )


def _settings_or_404(request: Request):
    settings = request.app.state.settings
    if not settings.webhook_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return settings


@router.get("/webhook")
def verify(request: Request) -> PlainTextResponse:
    """Meta's subscription check: echo `hub.challenge` if the verify token is ours."""
    settings = _settings_or_404(request)
    params = request.query_params
    token = params.get("hub.verify_token") or ""
    if params.get("hub.mode") != "subscribe" or not hmac.compare_digest(
        token.encode(), settings.webhook_verify_token.encode()
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    return PlainTextResponse(params.get("hub.challenge") or "")


async def _body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE)
    return bytes(body)


@router.post("/webhook")
async def notify(request: Request) -> PlainTextResponse:
    settings = _settings_or_404(request)
    body = await _body(request)
    if not signature_valid(settings.meta_app_secret, body, request.headers.get(SIGNATURE_HEADER)):
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    try:
        payload = json.loads(body)
    except ValueError:
        # Signed by Meta yet unreadable: retrying would not change it.
        log.warning("webhook notification is not JSON; ignored")
        return PlainTextResponse("ok")
    try:
        await run_in_threadpool(_process, request, payload)
    except Exception as exc:
        # Only the type: a message could quote the payload. 500 makes Meta retry, and the
        # events' unique key absorbs whatever part was already stored.
        log.error("webhook notification failed", extra={"error_type": type(exc).__name__})
        return PlainTextResponse("error", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return PlainTextResponse("ok")
