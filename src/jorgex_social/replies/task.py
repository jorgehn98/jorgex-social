"""Worker task for automatic replies: polls comments and direct messages, evaluates the rules on
them in memory, sends the replies and purges what is past retention.

Third-party text exists only in the `Comment` and `Message` objects of one poll: it is matched
against the rules and dropped, so the database and the logs only ever see identifiers. Each new
event is stored together with its deliveries in one transaction, so a crash never leaves an
event that would need its (gone) text to be evaluated again.

Sending follows the publishing worker: one short transaction before the call to Instagram and
one after, none during it. A reply whose outcome is unknown is never sent again; a duplicate
reply to a person is worse than a missing one.
"""

import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from jorgex_social.account import kill_switch
from jorgex_social.account.credential import connection_of
from jorgex_social.clock import Clock
from jorgex_social.instagram.errors import (
    AuthError,
    CredentialUnavailable,
    InstagramError,
    RateLimited,
    Refused,
)
from jorgex_social.instagram.types import Instagram
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
from jorgex_social.replies.rules import active_rules, first_match

log = logging.getLogger(__name__)

# instagram.md, «Mensajería»: one private reply per comment within 7 days; a direct message can
# be answered within 24 hours of the person's last message.
PRIVATE_REPLY_WINDOW = timedelta(days=7)
MESSAGING_WINDOW = timedelta(hours=24)
HOUR = timedelta(hours=1)
# Newest first; a week rarely holds more posts than this.
RECENT_MEDIA_TO_POLL = 50

# Nothing was sent and the cause affects every send: stop and try again on the next poll.
_RETRY_LATER = (CredentialUnavailable, AuthError, RateLimited)


@dataclass(frozen=True)
class _Send:
    delivery_id: str
    action: Action
    target: str  # the comment id, or the recipient's id for a message
    text: str


class ReplyTask:
    __name__ = "replies"

    def __init__(
        self,
        instagram: Instagram,
        clock: Clock,
        *,
        poll_every: timedelta = timedelta(seconds=120),
        hourly_cap: int = 200,
        retention: timedelta = timedelta(days=90),
        cooldown: timedelta = timedelta(minutes=60),
        choose: Callable[[Sequence[str]], str] = random.choice,
    ) -> None:
        self.instagram = instagram
        self.clock = clock
        self.poll_every = poll_every
        self.hourly_cap = hourly_cap
        self.retention = retention
        self.cooldown = cooldown
        self.choose = choose
        self._last_poll: datetime | None = None
        # Conversation id -> the activity already read. In memory: after a restart every recent
        # conversation is read once more, and the events' unique key drops what was seen.
        self._read: dict[str, datetime] = {}

    def __call__(self, sessions: sessionmaker) -> None:
        now = self.clock.now()
        if self._last_poll and now - self._last_poll < self.poll_every:
            return
        self._last_poll = now
        own = self._own_account(sessions)
        if own is None:
            log.warning("no connected account yet; not polling comments or messages")
        else:
            for name, step in (("comments", self.poll_comments), ("messages", self.poll_messages)):
                try:
                    step(sessions, own, now)
                except InstagramError as exc:
                    log.warning("reply poll failed", extra={"step": name, "error": str(exc)})
        self.deliver(sessions)
        self.purge(sessions, now)

    def _own_account(self, sessions: sessionmaker) -> str | None:
        with sessions() as session:
            connection = connection_of(session, self.instagram.network)
        return connection.external_account_id if connection else None

    # Polling

    def _known(self, session: Session, type_: Channel, refs: list[str]) -> set[str]:
        return set(
            session.scalars(
                select(Event.external_ref).where(
                    Event.network == self.instagram.network,
                    Event.type == type_,
                    Event.external_ref.in_(refs),
                )
            )
        )

    def poll_comments(self, sessions: sessionmaker, own: str, now: datetime) -> None:
        network = self.instagram.network
        media = [
            m
            for m in self.instagram.list_media(max_items=RECENT_MEDIA_TO_POLL)
            if now - m.timestamp < PRIVATE_REPLY_WINDOW
        ]
        for item in media:
            try:
                comments = self.instagram.list_comments(item.id)
            except _RETRY_LATER:
                raise
            except InstagramError as exc:
                log.warning("comments poll failed", extra={"media_ref": item.id, "error": str(exc)})
                continue
            # The account's own comments never fire a rule (no reply loops). Without an author
            # it cannot be told apart from one, so it is dropped too.
            others = [c for c in comments if c.author_id is not None and c.author_id != own]
            with sessions() as session, session.begin():
                known = self._known(session, Channel.COMMENT, [c.id for c in others])
                rules = active_rules(session, network, Channel.COMMENT)
                for comment in others:
                    if comment.id in known:
                        continue
                    rule = first_match(
                        rules, comment.text, media_ref=item.id, occurred_at=comment.timestamp
                    )
                    event = Event(
                        network=network,
                        type=Channel.COMMENT,
                        external_ref=comment.id,
                        media_ref=item.id,
                        occurred_at=comment.timestamp,
                        received_at=now,
                        rule_id=rule.id if rule else None,
                    )
                    self._record(session, event, rule, now)

    def poll_messages(self, sessions: sessionmaker, own: str, now: datetime) -> None:
        network = self.instagram.network
        for conversation in self.instagram.list_conversations(since=now - MESSAGING_WINDOW):
            if self._read.get(conversation.id) == conversation.updated_at:
                continue
            try:
                messages = self.instagram.list_messages(conversation.id)
            except _RETRY_LATER:
                raise
            except InstagramError as exc:
                log.warning("messages poll failed", extra={"error": str(exc)})
                continue
            # Only what the other person wrote and can still be answered; the account's own
            # messages (including these replies) never fire a rule.
            theirs = sorted(
                (
                    m
                    for m in messages
                    if m.sender_id is not None
                    and m.sender_id != own
                    and now - m.created_at < MESSAGING_WINDOW
                ),
                key=lambda m: m.created_at,
            )
            with sessions() as session, session.begin():
                known = self._known(session, Channel.MESSAGE, [m.id for m in theirs])
                rules = active_rules(session, network, Channel.MESSAGE)
                for message in theirs:
                    if message.id in known:
                        continue
                    person = self._conversation(session, message.sender_id, message.created_at, now)
                    rule = first_match(rules, message.text, occurred_at=message.created_at)
                    event = Event(
                        network=network,
                        type=Channel.MESSAGE,
                        external_ref=message.id,
                        conversation_id=person.id,
                        occurred_at=message.created_at,
                        received_at=now,
                        rule_id=rule.id if rule else None,
                    )
                    self._record(session, event, rule, now)
            self._read[conversation.id] = conversation.updated_at

    def _conversation(
        self, session: Session, person_id: str, written_at: datetime, now: datetime
    ) -> Conversation:
        network = self.instagram.network
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.network == network, Conversation.external_id == person_id
            )
        )
        if conversation is None:
            conversation = Conversation(
                network=network,
                external_id=person_id,
                last_user_message_at=written_at,
                window_until=written_at + MESSAGING_WINDOW,
                created_at=now,
                updated_at=now,
                purge_at=written_at + self.retention,
            )
            session.add(conversation)
            session.flush()
        elif written_at > conversation.last_user_message_at:
            # Every new message from the person restarts the 24-hour window.
            conversation.last_user_message_at = written_at
            conversation.window_until = written_at + MESSAGING_WINDOW
            conversation.purge_at = max(conversation.purge_at, written_at + self.retention)
            conversation.updated_at = now
        return conversation

    def _record(self, session: Session, event: Event, rule: Rule | None, now: datetime) -> None:
        """Stores the event and, if a rule fired, its deliveries, each with a variant picked now:
        replies are only ever the operator's own text, never anything from the event."""
        session.add(event)
        session.flush()
        if rule is None:
            return
        if event.type == Channel.COMMENT:
            planned = (
                (Action.PUBLIC_REPLY, rule.content.get("public_replies") or []),
                (Action.PRIVATE_REPLY, rule.content.get("private_replies") or []),
            )
        else:
            planned = ((Action.MESSAGE, rule.content.get("private_replies") or []),)
        for action, variants in planned:
            if variants:
                session.add(
                    Delivery(
                        event_id=event.id,
                        rule_id=rule.id,
                        action=action,
                        status=DeliveryStatus.PENDING,
                        text=self.choose(variants),
                        created_at=now,
                    )
                )
        log.info("rule matched", extra={"event_id": event.id, "rule_id": rule.id})

    # Sending

    def deliver(self, sessions: sessionmaker) -> None:
        with sessions() as session:
            pending = session.scalars(
                select(Delivery.id)
                .join(Event)
                .where(
                    Event.network == self.instagram.network,
                    Delivery.status == DeliveryStatus.PENDING,
                )
                .order_by(Delivery.created_at, Delivery.id)
            ).all()
        for delivery_id in pending:
            send = self._prepare(sessions, delivery_id)
            if send is not None and not self._send(sessions, send):
                return

    def _prepare(self, sessions: sessionmaker, delivery_id: str) -> _Send | None:
        """Checks, right before the call, everything that can forbid it, and marks the delivery
        attempted in the same commit. None when it must not be sent."""
        now = self.clock.now()
        with sessions() as session, session.begin():
            delivery = session.get_one(Delivery, delivery_id)
            if delivery.status != DeliveryStatus.PENDING:
                return None
            if delivery.attempted_at is not None:
                # A previous run died during the call: it may have gone out.
                self._finish(delivery, DeliveryStatus.FAILED, now, Reason.UNKNOWN)
                return None
            event = session.get_one(Event, delivery.event_id)
            conversation = (
                session.get_one(Conversation, event.conversation_id)
                if event.conversation_id
                else None
            )
            reason = self._blocked(session, delivery, event, conversation, now)
            if reason is not None:
                self._finish(delivery, DeliveryStatus.SKIPPED, now, reason)
                return None
            delivery.attempted_at = now
            action = Action(delivery.action)
            target = conversation.external_id if action == Action.MESSAGE else event.external_ref
            return _Send(delivery.id, action, target, delivery.text)

    def _blocked(
        self,
        session: Session,
        delivery: Delivery,
        event: Event,
        conversation: Conversation | None,
        now: datetime,
    ) -> Reason | None:
        if kill_switch.is_on(session):
            return Reason.KILL_SWITCH
        if conversation is not None and (
            conversation.paused_forever
            or (conversation.paused_until is not None and conversation.paused_until > now)
        ):
            return Reason.PAUSED
        match delivery.action:
            case Action.PRIVATE_REPLY:
                delivery.window_until_at_send = event.occurred_at + PRIVATE_REPLY_WINDOW
            case Action.MESSAGE:
                delivery.window_until_at_send = conversation.window_until
        if delivery.window_until_at_send is not None and delivery.window_until_at_send <= now:
            return Reason.WINDOW_CLOSED
        if conversation is not None:
            # Several matching messages in a row get one answer, not one each.
            recent = session.scalar(
                select(Delivery.id)
                .join(Event)
                .where(
                    Event.conversation_id == conversation.id,
                    Delivery.rule_id == delivery.rule_id,
                    Delivery.id != delivery.id,
                    Delivery.attempted_at > now - self.cooldown,
                )
            )
            if recent is not None:
                return Reason.COOLDOWN
        sent_last_hour = session.scalar(
            select(func.count())
            .select_from(Delivery)
            .join(Event)
            .where(Event.network == event.network, Delivery.attempted_at > now - HOUR)
        )
        if sent_last_hour >= self.hourly_cap:
            return Reason.HOURLY_CAP
        duplicate = session.scalar(
            select(Delivery.id).where(
                Delivery.event_id == delivery.event_id,
                Delivery.action == delivery.action,
                Delivery.id != delivery.id,
                Delivery.attempted_at.is_not(None),
            )
        )
        if duplicate is not None:
            return Reason.DUPLICATE
        return None

    def _send(self, sessions: sessionmaker, send: _Send) -> bool:
        """Returns False when sending should stop until the next poll."""
        try:
            match send.action:
                case Action.PUBLIC_REPLY:
                    result = self.instagram.reply_public(send.target, send.text)
                case Action.PRIVATE_REPLY:
                    result = self.instagram.reply_private(send.target, send.text)
                case Action.MESSAGE:
                    result = self.instagram.send_message(send.target, send.text)
        except _RETRY_LATER as exc:
            # Nothing went out: back to pending, and it is checked again before the next try.
            log.warning(
                "reply not sent; retrying on the next poll",
                extra={"delivery_id": send.delivery_id, "error": str(exc)},
            )
            with sessions() as session, session.begin():
                session.get_one(Delivery, send.delivery_id).attempted_at = None
            return False
        except Refused as exc:
            self._end(sessions, send, DeliveryStatus.FAILED, Reason.REFUSED, error=exc)
        except InstagramError as exc:
            self._end(sessions, send, DeliveryStatus.FAILED, Reason.UNKNOWN, error=exc)
        else:
            self._end(sessions, send, DeliveryStatus.SENT, result_ref=result)
        return True

    def _end(
        self,
        sessions: sessionmaker,
        send: _Send,
        status: DeliveryStatus,
        reason: Reason | None = None,
        *,
        error: InstagramError | None = None,
        result_ref: str | None = None,
    ) -> None:
        with sessions() as session, session.begin():
            delivery = session.get_one(Delivery, send.delivery_id)
            delivery.result_ref = result_ref
            if error is not None:
                delivery.error_code, delivery.error_subcode = error.code, error.subcode
            self._finish(delivery, status, self.clock.now(), reason)

    def _finish(
        self, delivery: Delivery, status: DeliveryStatus, now: datetime, reason: Reason | None
    ) -> None:
        delivery.status = status
        delivery.reason = reason
        delivery.finished_at = now
        log.info(
            "reply delivery finished",
            extra={
                "delivery_id": delivery.id,
                "action": delivery.action,
                "status": str(status),
                "reason": reason and str(reason),
            },
        )

    # Retention

    def purge(self, sessions: sessionmaker, now: datetime) -> None:
        """Events and deliveries older than the retention period, and conversations past their
        `purge_at` that nothing refers to any more. A paused conversation is kept while paused:
        dropping it would quietly lift the pause."""
        cutoff = now - self.retention
        with sessions() as session, session.begin():
            session.execute(delete(Delivery).where(Delivery.created_at < cutoff))
            session.execute(
                delete(Event).where(
                    Event.received_at < cutoff, Event.id.not_in(select(Delivery.event_id))
                )
            )
            session.execute(
                delete(Conversation).where(
                    Conversation.purge_at <= now,
                    Conversation.paused_forever.is_(False),
                    or_(Conversation.paused_until.is_(None), Conversation.paused_until <= now),
                    Conversation.id.not_in(
                        select(Event.conversation_id).where(Event.conversation_id.is_not(None))
                    ),
                )
            )
