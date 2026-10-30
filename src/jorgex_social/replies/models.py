"""Rules, the events they are evaluated on, the replies they cause and the conversations those
replies go to. No column holds third-party text or usernames: only identifiers, dates and state."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, mapped_column

from jorgex_social.db import Base, UTCDateTime


def _uuid() -> str:
    return str(uuid.uuid7())


class Channel(StrEnum):
    COMMENT = "comment"
    MESSAGE = "message"


class Source(StrEnum):
    """How an event reached the service first; the unique key drops it if it comes again."""

    POLL = "poll"
    WEBHOOK = "webhook"


class Action(StrEnum):
    PUBLIC_REPLY = "public_reply"
    PRIVATE_REPLY = "private_reply"
    MESSAGE = "message"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class Reason(StrEnum):
    """Why a delivery was skipped or failed."""

    KILL_SWITCH = "kill_switch"
    PAUSED = "paused"
    WINDOW_CLOSED = "window_closed"
    HOURLY_CAP = "hourly_cap"
    COOLDOWN = "cooldown"  # the rule already answered this person a moment ago
    ALREADY_ANSWERED = "already_answered"  # the rule already answered this person on this post
    REFUSED = "refused"  # Instagram said no; nothing was sent
    UNKNOWN = "unknown"  # no usable answer: it may have been sent, so it is never retried


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    network: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(100))
    channel: Mapped[str] = mapped_column(String(16))
    media_ref: Mapped[str | None] = mapped_column(String(64))  # null: every post
    priority: Mapped[int] = mapped_column(Integer)  # lower wins
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    # Only events after this moment can fire the rule, so activating it never answers old ones.
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Operator-written keywords and reply variants (replies/rules.py validates it).
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Conversation(Base):
    """A person who wrote to the account by direct message. `external_id` is their Instagram id,
    pseudonymous third-party data, so the row is purged like the events."""

    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("network", "external_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    network: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(64))
    last_user_message_at: Mapped[datetime] = mapped_column(UTCDateTime)
    window_until: Mapped[datetime] = mapped_column(UTCDateTime)
    paused_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    paused_forever: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    purge_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class Event(Base):
    """A comment or message by someone else, seen once. Its text was evaluated in memory and
    discarded; what remains is which rule, if any, it fired."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("network", "type", "external_ref"),
        Index("ix_events_network_media_ref_author_ref", "network", "media_ref", "author_ref"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    network: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(16))  # comment / message
    external_ref: Mapped[str] = mapped_column(String(64))
    media_ref: Mapped[str | None] = mapped_column(String(64))  # comments
    # Comments: the author's Instagram id, as pseudonymous as `Conversation.external_id`.
    author_ref: Mapped[str | None] = mapped_column(String(64))
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"))  # messages
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)  # Instagram's timestamp
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    rule_id: Mapped[str | None] = mapped_column(ForeignKey("rules.id"))  # null: no rule matched
    # Existing rows (before the webhook) came by polling.
    source: Mapped[str] = mapped_column(String(16), server_default=Source.POLL.value)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("rule_id", "event_id", "action"),
        # Instagram allows one private reply per comment; the database refuses a second one,
        # whatever the status of the first (a failed one may still have gone out).
        Index(
            "uq_deliveries_private_reply",
            "event_id",
            unique=True,
            sqlite_where=sql("action = 'private_reply'"),
        ),
        Index("ix_deliveries_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    rule_id: Mapped[str] = mapped_column(ForeignKey("rules.id"))
    action: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=DeliveryStatus.PENDING)
    reason: Mapped[str | None] = mapped_column(String(16))
    # The operator-written variant chosen, as it was when the event arrived.
    text: Mapped[str] = mapped_column(Text)
    window_until_at_send: Mapped[datetime | None] = mapped_column(UTCDateTime)
    result_ref: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[int | None] = mapped_column(Integer)
    error_subcode: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # Written before the call to Instagram: a delivery found pending with it set was interrupted
    # mid-send and may have gone out, so it is failed as unknown, never sent again.
    attempted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
