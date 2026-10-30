"""Where comments and messages enter the service, whether polling found them or the webhook was
notified of them: the same filters, the same deduplication, the same rule evaluation.

Third-party text exists only in the `Comment` and `Message` objects handed in: it is matched
against the rules and dropped, so the database only ever sees identifiers. Each new event is
stored together with its deliveries in the caller's transaction, so a crash never leaves an
event that would need its (gone) text to be evaluated again.
"""

import logging
import random
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from jorgex_social.account import audit
from jorgex_social.config import Settings
from jorgex_social.instagram.types import Comment, Message
from jorgex_social.replies.models import (
    Action,
    Channel,
    Conversation,
    Delivery,
    DeliveryStatus,
    Event,
    Rule,
    Source,
)
from jorgex_social.replies.rules import active_rules, first_match, opt_out_form

log = logging.getLogger(__name__)

# instagram.md, «Mensajería»: a direct message can be answered within 24 hours of the person's
# last message.
MESSAGING_WINDOW = timedelta(hours=24)
# Activity older than retention minus this margin is never evaluated: its event may already be
# purged, and seeing it again must not answer it again.
RETENTION_MARGIN = timedelta(days=1)


class Intake:
    def __init__(
        self,
        network: str,
        *,
        retention: timedelta = timedelta(days=90),
        choose: Callable[[Sequence[str]], str] = random.choice,
        opt_out_words: Iterable[str] = Settings.opt_out_words,
    ) -> None:
        self.network = network
        self.retention = retention
        self.choose = choose
        # Without the empty form: a message with no words is not a request to stop.
        self.opt_out_words = frozenset(filter(None, map(opt_out_form, opt_out_words)))

    def _known(self, session: Session, type_: Channel, refs: list[str]) -> set[str]:
        return set(
            session.scalars(
                select(Event.external_ref).where(
                    Event.network == self.network,
                    Event.type == type_,
                    Event.external_ref.in_(refs),
                )
            )
        )

    def _evaluable(self, occurred_at: datetime, now: datetime) -> bool:
        return now - occurred_at < self.retention - RETENTION_MARGIN

    def comments(
        self,
        session: Session,
        own: str,
        comments: Iterable[tuple[str, Comment]],
        now: datetime,
        source: Source,
    ) -> None:
        """Records the comments not seen before, each as (media id, comment)."""
        # The account's own comments never fire a rule (no reply loops). Without an author it
        # cannot be told apart from one, so it is dropped too.
        others = [
            (media_ref, c)
            for media_ref, c in comments
            if c.author_id is not None and c.author_id != own and self._evaluable(c.timestamp, now)
        ]
        known = self._known(session, Channel.COMMENT, [c.id for _, c in others])
        rules = active_rules(session, self.network, Channel.COMMENT)
        for media_ref, comment in others:
            if comment.id in known:
                continue
            known.add(comment.id)  # the same id twice in one batch is one event
            rule = first_match(
                rules, comment.text, media_ref=media_ref, occurred_at=comment.timestamp
            )
            event = Event(
                network=self.network,
                type=Channel.COMMENT,
                external_ref=comment.id,
                media_ref=media_ref,
                author_ref=comment.author_id,
                occurred_at=comment.timestamp,
                received_at=now,
                rule_id=rule.id if rule else None,
                source=source,
            )
            self._record(session, event, rule, now)

    def messages(
        self,
        session: Session,
        own: str,
        messages: Iterable[Message],
        now: datetime,
        source: Source,
    ) -> None:
        """Records the direct messages from other people not seen before."""
        # Only what the other person wrote and can still be answered; the account's own
        # messages (including these replies) never fire a rule.
        theirs = sorted(
            (
                m
                for m in messages
                if m.sender_id is not None
                and m.sender_id != own
                and now - m.created_at < MESSAGING_WINDOW
                and self._evaluable(m.created_at, now)
            ),
            key=lambda m: m.created_at,
        )
        ids = [m.id for m in theirs]
        known = self._known(session, Channel.MESSAGE, ids)
        # Our own sends, in case Instagram names their sender with an id other than the one
        # the connection holds.
        known |= set(
            session.scalars(select(Delivery.result_ref).where(Delivery.result_ref.in_(ids)))
        )
        rules = active_rules(session, self.network, Channel.MESSAGE)
        for message in theirs:
            if message.id in known:
                continue
            known.add(message.id)
            person = self._conversation(session, message.sender_id, message.created_at, now)
            rule = None
            if opt_out_form(message.text) in self.opt_out_words:
                self._opt_out(session, person, now)
            else:
                rule = first_match(rules, message.text, occurred_at=message.created_at)
            event = Event(
                network=self.network,
                type=Channel.MESSAGE,
                external_ref=message.id,
                conversation_id=person.id,
                occurred_at=message.created_at,
                received_at=now,
                rule_id=rule.id if rule else None,
                source=source,
            )
            self._record(session, event, rule, now)

    def _opt_out(self, session: Session, person: Conversation, now: datetime) -> None:
        """Nothing is sent back, not even a confirmation. Replies still pending end as paused
        when their turn to be sent comes."""
        person.paused_forever = True
        person.paused_until = None
        person.updated_at = now
        audit.record(
            session,
            now,
            "conversation.opted_out",
            object_type="conversation",
            object_id=person.id,
        )

    def _conversation(
        self, session: Session, person_id: str, written_at: datetime, now: datetime
    ) -> Conversation:
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.network == self.network, Conversation.external_id == person_id
            )
        )
        if conversation is None:
            conversation = Conversation(
                network=self.network,
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
        log.info(
            "rule matched",
            extra={"event_id": event.id, "rule_id": rule.id, "source": str(event.source)},
        )
