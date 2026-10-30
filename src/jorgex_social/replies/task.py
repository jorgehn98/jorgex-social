"""Worker task for automatic replies: polls comments and direct messages and hands them to the
intake (replies/intake.py, shared with the webhook), sends the replies and purges what is past
retention.

Polling runs every `poll_every`; sending runs on every worker pass, so a reply to something the
webhook recorded goes out within one pass instead of waiting for the next poll.

Sending follows the publishing worker: one short transaction before the call to Instagram and
one after, none during it. A reply whose outcome is unknown is never sent again; a duplicate
reply to a person is worse than a missing one.
"""

import logging
import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from jorgex_social.account import incidents, kill_switch
from jorgex_social.account.credential import connection_of
from jorgex_social.account.models import IncidentType
from jorgex_social.clock import Clock
from jorgex_social.config import Settings
from jorgex_social.instagram.errors import (
    AccountRestricted,
    AuthError,
    CredentialUnavailable,
    InstagramError,
    RateLimited,
    Refused,
)
from jorgex_social.instagram.types import Instagram
from jorgex_social.replies.intake import MESSAGING_WINDOW, Intake
from jorgex_social.replies.models import (
    Action,
    Channel,
    Conversation,
    Delivery,
    DeliveryStatus,
    Event,
    Reason,
    Source,
)
from jorgex_social.replies.rules import active_rules

log = logging.getLogger(__name__)

# instagram.md, «Mensajería»: one private reply per comment within 7 days.
PRIVATE_REPLY_WINDOW = timedelta(days=7)
# Our own limit, not Meta's, counted from the comment.
PUBLIC_REPLY_MAX_AGE = timedelta(hours=24)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
# Comments are polled on media up to this old (newest first, at most this many), plus any media
# an active rule names. The 7-day window for a private reply counts from the comment, not the post.
RECENT_MEDIA = timedelta(days=30)
RECENT_MEDIA_TO_POLL = 50
# webhook_silent: the webhook has delivered nothing for this long while polling found activity
# from the same period.
WEBHOOK_SILENCE = timedelta(hours=24)

# Nothing was sent and the cause affects every send: stop and try again on the next poll.
_RETRY_LATER = (CredentialUnavailable, AuthError, RateLimited)
# A poll goes through many media and conversations; on these, the rest would only fail the same way.
_STOP_POLL = (*_RETRY_LATER, AccountRestricted)


def _log_failure(what: str, exc: Exception, **extra) -> None:
    """Only an InstagramError's message is logged (it never quotes a comment or a message); for
    anything else, only its type, since a parsing or database error could quote the third-party
    text being handled."""
    if isinstance(exc, InstagramError):
        log.warning(f"{what} failed", extra={**extra, "error": str(exc)})
    else:
        log.error(f"{what} failed", extra={**extra, "error_type": type(exc).__name__})


def _isolated(what: str, step: Callable[..., None], *args) -> None:
    """Runs one step of the cycle so that its failure stops none of the others."""
    try:
        step(*args)
    except Exception as exc:
        _log_failure(what, exc)


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
        hourly_public_cap: int = 30,
        per_minute: int = 12,
        retention: timedelta = timedelta(days=90),
        cooldown: timedelta = timedelta(minutes=60),
        choose: Callable[[Sequence[str]], str] = random.choice,
        webhook: bool = False,
        opt_out_words: Iterable[str] = Settings.opt_out_words,
    ) -> None:
        self.instagram = instagram
        self.clock = clock
        self.poll_every = poll_every
        self.hourly_cap = hourly_cap
        self.hourly_public_cap = hourly_public_cap
        self.per_minute = per_minute
        self.retention = retention
        self.cooldown = cooldown
        self.webhook = webhook
        self.intake = Intake(
            instagram.network, retention=retention, choose=choose, opt_out_words=opt_out_words
        )
        self._started = clock.now()
        self._last_poll: datetime | None = None
        # Set when a send failed in a way that affects every send (rate limit, credential,
        # restriction): sending waits for the next poll instead of hitting Instagram on every pass.
        self._hold_sends = False
        # Conversation id -> the activity already read. In memory: after a restart every recent
        # conversation is read once more, and the events' unique key drops what was seen.
        self._read: dict[str, datetime] = {}

    def __call__(self, sessions: sessionmaker) -> None:
        now = self.clock.now()
        polling = self._last_poll is None or now - self._last_poll >= self.poll_every
        if polling:
            self._last_poll = now
            self._hold_sends = False
            own = self._own_account(sessions)
            if own is None:
                log.warning("no connected account yet; not polling comments or messages")
            else:
                for name, step in (
                    ("comments", self.poll_comments),
                    ("messages", self.poll_messages),
                ):
                    _isolated(f"{name} poll", self._poll, step, sessions, own, now)
        if not self._hold_sends:
            _isolated("delivery", self.deliver, sessions)
        if polling:
            _isolated("purge", self.purge, sessions, now)
            _isolated("webhook health", self.watch_webhook, sessions, now)

    def _own_account(self, sessions: sessionmaker) -> str | None:
        with sessions() as session:
            connection = connection_of(session, self.instagram.network)
        return connection.external_account_id if connection else None

    # Polling

    def _poll(
        self,
        step: Callable[[sessionmaker, str, datetime], None],
        sessions: sessionmaker,
        own: str,
        now: datetime,
    ) -> None:
        try:
            step(sessions, own, now)
        except AccountRestricted as exc:
            with sessions() as session, session.begin():
                kill_switch.trip(session, self.instagram.network, exc, self.clock.now())
            raise

    def poll_comments(self, sessions: sessionmaker, own: str, now: datetime) -> None:
        recent = [
            m.id
            for m in self.instagram.list_media(max_items=RECENT_MEDIA_TO_POLL)
            if now - m.timestamp < RECENT_MEDIA
        ]
        with sessions() as session:
            named = {
                r.media_ref for r in active_rules(session, self.instagram.network, Channel.COMMENT)
            }
        # One odd media never stops the others; a failure that affects every call stops the poll.
        for media_ref in recent + sorted(named - {None} - set(recent)):
            try:
                self._poll_media(sessions, own, now, media_ref)
            except _STOP_POLL:
                raise
            except Exception as exc:
                _log_failure("comments poll", exc, media_ref=media_ref)

    def _poll_media(self, sessions: sessionmaker, own: str, now: datetime, media_ref: str) -> None:
        comments = self.instagram.list_comments(media_ref)
        with sessions() as session, session.begin():
            self.intake.comments(session, own, [(media_ref, c) for c in comments], now, Source.POLL)

    def poll_messages(self, sessions: sessionmaker, own: str, now: datetime) -> None:
        for conversation in self.instagram.list_conversations(since=now - MESSAGING_WINDOW):
            if self._read.get(conversation.id) == conversation.updated_at:
                continue
            try:
                self._poll_conversation(sessions, own, now, conversation.id)
            except _STOP_POLL:
                raise
            except Exception as exc:
                _log_failure("messages poll", exc)
            else:
                self._read[conversation.id] = conversation.updated_at

    def _poll_conversation(
        self, sessions: sessionmaker, own: str, now: datetime, conversation_id: str
    ) -> None:
        messages = self.instagram.list_messages(conversation_id)
        with sessions() as session, session.begin():
            self.intake.messages(session, own, messages, now, Source.POLL)

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
            # At the pace limit the rest stay pending, untouched, for a later pass.
            if self._hold_sends or self._at_pace_limit(sessions):
                return
            try:
                send = self._prepare(sessions, delivery_id)
                if send is not None:
                    self._send(sessions, send)
            except Exception as exc:
                # Left as it was: if attempted_at was written, the next pass fails it as unknown.
                _log_failure("delivery", exc, delivery_id=delivery_id)

    def _at_pace_limit(self, sessions: sessionmaker) -> bool:
        with sessions() as session:
            return self._attempted_since(session, self.clock.now() - MINUTE) >= self.per_minute

    def _attempted_since(
        self, session: Session, since: datetime, action: Action | None = None
    ) -> int:
        query = (
            select(func.count())
            .select_from(Delivery)
            .join(Event)
            .where(Event.network == self.instagram.network, Delivery.attempted_at > since)
        )
        return session.scalar(query if action is None else query.where(Delivery.action == action))

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
        person = conversation
        if event.author_ref is not None:
            # Finds the pause only if Instagram names a comment's author and a message's sender
            # with the same id: seen matching in one test, not documented by Meta. If it does not
            # match, this finds nobody.
            person = session.scalar(
                select(Conversation).where(
                    Conversation.network == event.network,
                    Conversation.external_id == event.author_ref,
                )
            )
        if person is not None and (
            person.paused_forever or (person.paused_until is not None and person.paused_until > now)
        ):
            return Reason.PAUSED
        match delivery.action:
            case Action.PUBLIC_REPLY:
                delivery.window_until_at_send = event.occurred_at + PUBLIC_REPLY_MAX_AGE
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
        if event.author_ref is not None:
            # One person repeating the keyword on a post is answered once by each rule. Lasts as
            # long as the rows are retained. A refused reply proves nothing went out, so it does
            # not count; an unknown one may have.
            answered = session.scalar(
                select(Delivery.id)
                .join(Event)
                .where(
                    Event.network == event.network,
                    Event.media_ref == event.media_ref,
                    Event.author_ref == event.author_ref,
                    Delivery.rule_id == delivery.rule_id,
                    Delivery.action == delivery.action,
                    Delivery.id != delivery.id,
                    Delivery.attempted_at.is_not(None),
                    # Not `!=`: a reply sent or in flight has no reason, and NULL would not match.
                    Delivery.reason.is_distinct_from(Reason.REFUSED),
                )
            )
            if answered is not None:
                return Reason.ALREADY_ANSWERED
        if self._attempted_since(session, now - HOUR) >= self.hourly_cap:
            return Reason.HOURLY_CAP
        if (
            delivery.action == Action.PUBLIC_REPLY
            and self._attempted_since(session, now - HOUR, Action.PUBLIC_REPLY)
            >= self.hourly_public_cap
        ):
            return Reason.HOURLY_CAP
        # No duplicate check here: one rule per event, so a second delivery of the same action
        # cannot be created. The unique indexes on deliveries are the safety net.
        return None

    def _send(self, sessions: sessionmaker, send: _Send) -> None:
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
            self._hold_sends = True
        except AccountRestricted as exc:
            # Set first: if the commit below fails, the next delivery must still not be sent.
            self._hold_sends = True
            self._end(sessions, send, DeliveryStatus.FAILED, Reason.REFUSED, error=exc)
        except Refused as exc:
            self._end(sessions, send, DeliveryStatus.FAILED, Reason.REFUSED, error=exc)
        except InstagramError as exc:
            self._end(sessions, send, DeliveryStatus.FAILED, Reason.UNKNOWN, error=exc)
        else:
            self._end(sessions, send, DeliveryStatus.SENT, result_ref=result)

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
            if isinstance(error, AccountRestricted):
                kill_switch.trip(session, self.instagram.network, error, self.clock.now())

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

    # Webhook health

    def watch_webhook(self, sessions: sessionmaker, now: datetime) -> None:
        """Opens webhook_silent when the webhook is configured, has delivered nothing for a day
        (counting from this worker's start if it never has), and polling found a comment or a
        message from that day first. A valid notification closes it (replies/webhook.py); so does
        turning the webhook off."""
        network = self.instagram.network
        with sessions() as session, session.begin():
            if not self.webhook:
                incidents.sync(session, network, IncidentType.WEBHOOK_SILENT, False, now)
                return
            connection = connection_of(session, network)
            silent_since = (connection and connection.webhook_notified_at) or self._started
            if now - silent_since < WEBHOOK_SILENCE:
                return
            missed = session.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.network == network,
                    Event.source == Source.POLL,
                    Event.occurred_at > now - WEBHOOK_SILENCE,
                )
            )
            if missed:
                incidents.sync(
                    session,
                    network,
                    IncidentType.WEBHOOK_SILENT,
                    True,
                    now,
                    {"polled_events": missed},
                )
