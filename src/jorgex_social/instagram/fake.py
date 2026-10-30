from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import count
from typing import Any

from jorgex_social.clock import Clock
from jorgex_social.instagram.client import check_insights_window
from jorgex_social.instagram.errors import AuthError, InstagramError, Refused
from jorgex_social.instagram.types import (
    Account,
    Comment,
    ContainerKind,
    ContainerRequest,
    ContainerStatus,
    Conversation,
    Instagram,
    Media,
    Message,
    ProductType,
    PublishingLimit,
    Renewal,
)

# Documented limits (docs/research/references/instagram.md); the fake enforces them so the
# service meets them in simulation before it meets them in production.
CONTAINER_LIFETIME = timedelta(hours=24)
STORY_INSIGHTS_LIFETIME = timedelta(hours=24)
MAX_CAROUSEL_ITEMS = 20
PUBLISHING_WINDOW = timedelta(hours=24)
CREDENTIAL_LIFETIME = timedelta(days=60)
MIN_CREDENTIAL_AGE_TO_RENEW = timedelta(hours=24)
PRIVATE_REPLY_WINDOW = timedelta(days=7)
MESSAGING_WINDOW = timedelta(hours=24)
MAX_MESSAGE_BYTES = 1000
MAX_COMMENT_CHARS = 2200
MESSAGES_READABLE = 20
# The simulated account. Comments and messages injected for tests come from other ids.
ACCOUNT_ID = "17841400000000000"

_PRODUCT = {
    ContainerKind.IMAGE: ProductType.FEED,
    ContainerKind.CAROUSEL: ProductType.FEED,
    ContainerKind.REELS: ProductType.REELS,
    ContainerKind.STORIES: ProductType.STORY,
}
_MEDIA_TYPE = {
    ContainerKind.IMAGE: "IMAGE",
    ContainerKind.CAROUSEL: "CAROUSEL_ALBUM",
    ContainerKind.REELS: "VIDEO",
}


@dataclass
class _Container:
    request: ContainerRequest
    created_at: datetime
    ready_at: datetime
    error: bool = False
    media_id: str | None = None


@dataclass
class _Comment:
    media_id: str
    comment: Comment
    private_reply_sent: bool = False


@dataclass
class _Thread:
    """The conversation between the account and one person."""

    id: str
    person_id: str
    messages: list[Message] = field(default_factory=list)


@dataclass
class _Failure:
    error: InstagramError
    after: bool  # True: the call takes effect and then fails, like a 5xx after going live


class FakeInstagram:
    """Stateful, in-memory Instagram with a controllable clock. All data is synthetic.

    Video containers take `video_processing` to become FINISHED; images are ready at once.
    `fail_next` injects errors, including the ones that leave a write's outcome unknown.
    `add_comment` and `add_message` inject synthetic third-party activity; `sent` records every
    reply and message the account sends, as (operation, target, text).

    The credential is untracked (never expires) until it is first renewed or a
    `credential_issued_at` is given; from then on it expires after `credential_lifetime` unless
    renewed, renewal follows Meta's rules, and `revoke()` makes every call fail as Meta does.
    """

    network = "simulation"

    def __init__(
        self,
        clock: Clock,
        *,
        video_processing: timedelta = timedelta(seconds=30),
        quota_total: int = 100,
        credential_issued_at: datetime | None = None,
        credential_lifetime: timedelta = CREDENTIAL_LIFETIME,
    ) -> None:
        self.clock = clock
        self.video_processing = video_processing
        self.quota_total = quota_total
        self.credential_issued_at = credential_issued_at
        self.credential_lifetime = credential_lifetime
        self.revoked = False
        self._renewals = count(1)
        self._ids = count(17_000_000_000_000_001)
        self._containers: dict[str, _Container] = {}
        self._media: dict[str, tuple[Media, ContainerKind]] = {}
        self._failures: dict[str, list[_Failure]] = {}
        self._comments: dict[str, _Comment] = {}
        self._threads: dict[str, _Thread] = {}  # by person id
        self.sent: list[tuple[str, str, str]] = []

    # Test and simulation controls

    def fail_next(self, operation: str, error: InstagramError, *, after: bool = False) -> None:
        if operation not in Instagram.__dict__:
            raise ValueError(f"unknown operation {operation!r}")
        self._failures.setdefault(operation, []).append(_Failure(error, after))

    def revoke(self) -> None:
        self.revoked = True

    @property
    def credential_expires_at(self) -> datetime | None:
        if self.credential_issued_at is None:
            return None
        return self.credential_issued_at + self.credential_lifetime

    def _check_credential(self) -> None:
        if self.revoked:
            raise AuthError("Instagram API 400: the access token was revoked", code=190)
        expires_at = self.credential_expires_at
        if expires_at is not None and self.clock.now() >= expires_at:
            raise AuthError("Instagram API 400: the access token has expired", code=190)

    def add_comment(
        self,
        media_id: str,
        text: str,
        *,
        author_id: str = "17900000000000001",
        at: datetime | None = None,
    ) -> str:
        self._get_media(media_id)
        comment = Comment(self._new_id(), author_id, at or self.clock.now(), text)
        self._comments[comment.id] = _Comment(media_id, comment)
        return comment.id

    def add_message(self, sender_id: str, text: str, *, at: datetime | None = None) -> str:
        message = Message(self._new_id(), sender_id, at or self.clock.now(), text)
        self._thread(sender_id).messages.append(message)
        return message.id

    def _thread(self, person_id: str) -> _Thread:
        if person_id not in self._threads:
            self._threads[person_id] = _Thread(self._new_id(), person_id)
        return self._threads[person_id]

    def mark_error(self, container_id: str) -> None:
        self._containers[container_id].error = True

    def _failure(self, operation: str) -> _Failure | None:
        queue = self._failures.get(operation)
        return queue.pop(0) if queue else None

    def _run(self, operation: str, action):
        failure = self._failure(operation)
        if failure and not failure.after:
            raise failure.error
        self._check_credential()
        result = action()
        if failure:
            raise failure.error
        return result

    def _new_id(self) -> str:
        return str(next(self._ids))

    # Instagram interface

    def account(self) -> Account:
        return self._run(
            "account",
            lambda: Account(
                id=ACCOUNT_ID,
                username="simulated_account",
                account_type="MEDIA_CREATOR",
                followers_count=1000,
                media_count=sum(1 for _, k in self._media.values() if k != ContainerKind.STORIES),
            ),
        )

    def publishing_limit(self) -> PublishingLimit:
        return self._run(
            "publishing_limit",
            lambda: PublishingLimit(
                quota_usage=self._published_in_window(),
                quota_total=self.quota_total,
                quota_duration_seconds=int(PUBLISHING_WINDOW.total_seconds()),
            ),
        )

    def create_container(self, request: ContainerRequest) -> str:
        return self._run("create_container", lambda: self._create(request))

    def _create(self, request: ContainerRequest) -> str:
        if request.kind == ContainerKind.CAROUSEL:
            if not 2 <= len(request.children) <= MAX_CAROUSEL_ITEMS:
                raise Refused(f"carousel needs 2 to {MAX_CAROUSEL_ITEMS} children", code=100)
            for child in request.children:
                container = self._containers.get(child)
                if container is None or not container.request.is_carousel_item:
                    raise Refused(f"{child} is not a carousel item container", code=100)
                if self._status(container) != ContainerStatus.FINISHED:
                    raise Refused(f"carousel item {child} is not ready", code=9007)
        elif not request.url:
            raise Refused("media url is required", code=100)
        elif request.kind == ContainerKind.VIDEO and not request.is_carousel_item:
            raise Refused("VIDEO containers are only for carousel items; use REELS", code=100)
        now = self.clock.now()
        is_video = request.kind in {ContainerKind.REELS, ContainerKind.VIDEO} or request.is_video
        container_id = self._new_id()
        self._containers[container_id] = _Container(
            request=request,
            created_at=now,
            ready_at=now + (self.video_processing if is_video else timedelta(0)),
        )
        return container_id

    def container_status(self, container_id: str) -> ContainerStatus:
        return self._run("container_status", lambda: self._status(self._container(container_id)))

    def _container(self, container_id: str) -> _Container:
        container = self._containers.get(container_id)
        if container is None:
            raise Refused(f"unknown container {container_id}", code=100)
        return container

    def _status(self, container: _Container) -> ContainerStatus:
        now = self.clock.now()
        if container.media_id:
            return ContainerStatus.PUBLISHED
        if container.error:
            return ContainerStatus.ERROR
        if now - container.created_at > CONTAINER_LIFETIME:
            return ContainerStatus.EXPIRED
        if now < container.ready_at:
            return ContainerStatus.IN_PROGRESS
        return ContainerStatus.FINISHED

    def publish(self, container_id: str) -> str:
        return self._run("publish", lambda: self._publish(container_id))

    def _publish(self, container_id: str) -> str:
        container = self._container(container_id)
        status = self._status(container)
        if status != ContainerStatus.FINISHED or container.request.is_carousel_item:
            raise Refused(f"container {container_id} cannot be published ({status})", code=9007)
        if self._published_in_window() >= self.quota_total:
            raise Refused("publishing quota reached", code=9)
        kind = container.request.kind
        media_id = self._new_id()
        container.media_id = media_id
        self._media[media_id] = (
            Media(
                id=media_id,
                product_type=_PRODUCT[kind],
                media_type=_MEDIA_TYPE.get(
                    kind, "VIDEO" if container.request.is_video else "IMAGE"
                ),
                permalink=f"https://example.invalid/media/{media_id}",
                timestamp=self.clock.now(),
                caption=container.request.caption,
            ),
            kind,
        )
        return media_id

    def _published_in_window(self) -> int:
        since = self.clock.now() - PUBLISHING_WINDOW
        return sum(1 for media, _ in self._media.values() if media.timestamp > since)

    def media(self, media_id: str) -> Media:
        return self._run("media", lambda: self._get_media(media_id))

    def _get_media(self, media_id: str) -> Media:
        if media_id not in self._media:
            raise Refused(f"unknown media {media_id}", code=100)
        return self._media[media_id][0]

    def list_media(self, max_items: int | None = None) -> list[Media]:
        def newest_first() -> list[Media]:
            # Like the real endpoint, stories are not listed.
            items = [m for m, kind in self._media.values() if kind != ContainerKind.STORIES]
            items.sort(key=lambda m: m.timestamp, reverse=True)
            return items if max_items is None else items[:max_items]

        return self._run("list_media", newest_first)

    def media_insights(self, media_id: str, product_type: ProductType) -> dict[str, Any]:
        return self._run("media_insights", lambda: self._insights(media_id, product_type))

    def _insights(self, media_id: str, product_type: ProductType) -> dict[str, Any]:
        media = self._get_media(media_id)
        if product_type != media.product_type:
            # The real API rejects the whole request when a metric does not apply to the media.
            raise Refused(f"metrics for {product_type} do not apply to this media", code=100)
        age = self.clock.now() - media.timestamp
        if product_type == ProductType.STORY and age > STORY_INSIGHTS_LIFETIME:
            raise Refused("story insights are only available for 24 hours", code=10)
        seed = int(media_id) % 97
        values: dict[str, Any] = {"reach": 100 + seed, "views": 150 + seed, "shares": seed % 7}
        if product_type != ProductType.STORY:
            values |= {"likes": 10 + seed % 13, "comments": seed % 5, "saved": seed % 3}
        if product_type == ProductType.REELS:
            values["ig_reels_avg_watch_time"] = 4000 + seed * 10
        return values

    def account_insights(self, since: datetime, until: datetime) -> dict[str, Any]:
        check_insights_window(since, until)
        days = max(1, (until - since).days)
        return self._run(
            "account_insights",
            lambda: {"reach": 300 * days, "views": 500 * days, "accounts_engaged": 20 * days},
        )

    def list_comments(self, media_id: str) -> list[Comment]:
        def comments() -> list[Comment]:
            self._get_media(media_id)
            found = [c.comment for c in self._comments.values() if c.media_id == media_id]
            return sorted(found, key=lambda c: c.timestamp, reverse=True)

        return self._run("list_comments", comments)

    def list_conversations(self, since: datetime | None = None) -> list[Conversation]:
        def conversations() -> list[Conversation]:
            found = [
                Conversation(t.id, max(m.created_at for m in t.messages))
                for t in self._threads.values()
                if t.messages
            ]
            found.sort(key=lambda c: c.updated_at, reverse=True)
            return [c for c in found if since is None or c.updated_at >= since]

        return self._run("list_conversations", conversations)

    def list_messages(self, conversation_id: str) -> list[Message]:
        def messages() -> list[Message]:
            for thread in self._threads.values():
                if thread.id == conversation_id:
                    newest = sorted(thread.messages, key=lambda m: m.created_at, reverse=True)
                    return newest[:MESSAGES_READABLE]
            raise Refused(f"unknown conversation {conversation_id}", code=100)

        return self._run("list_messages", messages)

    def _comment(self, comment_id: str) -> _Comment:
        if comment_id not in self._comments:
            raise Refused(f"unknown comment {comment_id}", code=100)
        return self._comments[comment_id]

    def _check_message(self, text: str) -> None:
        if not text or len(text.encode()) > MAX_MESSAGE_BYTES:
            raise Refused(f"message text must be 1 to {MAX_MESSAGE_BYTES} bytes", code=100)

    def _record(self, operation: str, target: str, text: str) -> str:
        self.sent.append((operation, target, text))
        return self._new_id()

    def reply_public(self, comment_id: str, text: str) -> str:
        def reply() -> str:
            self._comment(comment_id)
            if not text or len(text) > MAX_COMMENT_CHARS:
                raise Refused(f"reply must be 1 to {MAX_COMMENT_CHARS} characters", code=100)
            return self._record("reply_public", comment_id, text)

        return self._run("reply_public", reply)

    def reply_private(self, comment_id: str, text: str) -> str:
        def reply() -> str:
            stored = self._comment(comment_id)
            self._check_message(text)
            if stored.private_reply_sent:
                raise Refused("only one private reply per comment", code=10, subcode=2534023)
            if self.clock.now() - stored.comment.timestamp > PRIVATE_REPLY_WINDOW:
                raise Refused("the comment is older than 7 days", code=10, subcode=2534022)
            stored.private_reply_sent = True
            message_id = self._record("reply_private", comment_id, text)
            # Like Instagram: the reply opens (or continues) a conversation with the author.
            if stored.comment.author_id:
                self._thread(stored.comment.author_id).messages.append(
                    Message(message_id, ACCOUNT_ID, self.clock.now(), text)
                )
            return message_id

        return self._run("reply_private", reply)

    def send_message(self, recipient_id: str, text: str) -> str:
        def send() -> str:
            self._check_message(text)
            thread = self._threads.get(recipient_id)
            last = max(
                (
                    m.created_at
                    for m in (thread.messages if thread else [])
                    if m.sender_id == recipient_id
                ),
                default=None,
            )
            if last is None or self.clock.now() - last > MESSAGING_WINDOW:
                raise Refused("outside the 24-hour messaging window", code=10, subcode=2018278)
            message_id = self._record("send_message", recipient_id, text)
            thread.messages.append(Message(message_id, ACCOUNT_ID, self.clock.now(), text))
            return message_id

        return self._run("send_message", send)

    def check_credential(self) -> None:
        # Nothing to read in simulation; `fail_next` can still inject a CredentialUnavailable.
        failure = self._failure("check_credential")
        if failure:
            raise failure.error

    def refresh_token(self) -> Renewal:
        return self._run("refresh_token", self._refresh)

    def _refresh(self) -> Renewal:
        now = self.clock.now()
        issued_at = self.credential_issued_at
        if issued_at is not None and now - issued_at < MIN_CREDENTIAL_AGE_TO_RENEW:
            raise Refused("Instagram API 400: the token must be at least 24 hours old to renew")
        self.credential_issued_at = now
        return Renewal(f"simulated-token-{next(self._renewals)}", self.credential_lifetime)
