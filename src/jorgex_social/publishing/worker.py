"""Worker task that publishes due posts and settles attempts whose outcome is unknown.

Every step commits in its own short transaction and no transaction is open during a call to
Instagram, so a crash at any point leaves rows the next pass can resolve.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, union
from sqlalchemy.orm import Session, sessionmaker

from jorgex_social.clock import Clock
from jorgex_social.instagram.errors import InstagramError, Refused
from jorgex_social.instagram.types import (
    ContainerKind,
    ContainerRequest,
    ContainerStatus,
    Instagram,
    ProductType,
)
from jorgex_social.instagram.types import Media as PublishedMedia
from jorgex_social.publishing.media import (
    KEEP_AFTER_PUBLISH,
    issue_url,
    path_of,
    public_url,
    revoke_url,
)
from jorgex_social.publishing.models import (
    AttemptResult,
    Media,
    MediaType,
    Post,
    PostItem,
    PostStatus,
    PostType,
    PublishAttempt,
    Resolution,
)

log = logging.getLogger(__name__)

# Like the helper this replaces: Meta suggests polling once a minute for five minutes, but a
# video can take longer.
POLL_INTERVAL = timedelta(seconds=15)
POLL_TIMEOUT = timedelta(minutes=10)
# An attempt still in progress after this long belongs to a worker that died mid-way.
STALE_ATTEMPT = timedelta(minutes=15)
# Instagram timestamps have whole seconds, so a post can look slightly older than its attempt.
TIMESTAMP_SLACK = timedelta(minutes=1)
RECENT_MEDIA_TO_SEARCH = 50
# The fake never downloads anything, so simulation needs no real public URL.
SIMULATED_PUBLIC_URL = "https://simulation.invalid"

# Posts whose media must stay on disk.
ACTIVE = (
    PostStatus.DRAFT,
    PostStatus.SCHEDULED,
    PostStatus.PUBLISHING,
    PostStatus.OUTCOME_UNKNOWN,
)


class ContainerFailed(Exception):
    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


@dataclass(frozen=True)
class _Plan:
    """What one attempt sends to Instagram, read in the transaction that starts it."""

    attempt_id: str
    type: PostType
    caption: str
    items: list[tuple[bool, str]]  # (is_video, url) in order
    cover_url: str | None
    thumb_offset_ms: int | None


@dataclass(frozen=True)
class _Pending:
    attempt_id: str
    container_ref: str | None
    started_at: datetime
    type: PostType
    caption: str


class PublishTask:
    __name__ = "publish"

    def __init__(
        self,
        instagram: Instagram,
        clock: Clock,
        media_dir: Path,
        public_base_url: str | None,
        *,
        grace: timedelta = timedelta(minutes=30),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.instagram = instagram
        self.clock = clock
        self.media_dir = media_dir
        self.public_base_url = public_base_url
        self.grace = grace
        self.sleep = sleep

    def __call__(self, sessions: sessionmaker) -> None:
        if not self.public_base_url:
            raise RuntimeError(
                "cannot publish: set JORGEX_SOCIAL_PUBLIC_URL, Instagram downloads media from it"
            )
        self.publish_due(sessions)
        self.resolve(sessions)
        self.purge(sessions)

    # Due posts

    def publish_due(self, sessions: sessionmaker) -> None:
        now = self.clock.now()
        network = self.instagram.network
        with sessions() as session, session.begin():
            missed = session.scalars(
                select(Post).where(
                    Post.network == network,
                    Post.status == PostStatus.SCHEDULED,
                    Post.scheduled_for < now - self.grace,
                )
            )
            for post in missed:
                post.status = PostStatus.FAILED
                post.failure = "missed_schedule"
                log.warning("missed schedule", extra={"post_id": post.id})
        while (plan := self._start(sessions, now)) is not None:
            self._attempt(sessions, plan)

    def _start(self, sessions: sessionmaker, now: datetime) -> _Plan | None:
        """Marks the next due post publishing, with an attempt and public URLs, in one commit."""
        with sessions() as session, session.begin():
            post = session.scalar(
                select(Post)
                .where(
                    Post.network == self.instagram.network,
                    Post.status == PostStatus.SCHEDULED,
                    Post.scheduled_for <= now,
                )
                .order_by(Post.scheduled_for)
                .limit(1)
            )
            if post is None:
                return None
            attempt = self._new_attempt(session, post)
            media = {m.id: m for m in self._media_of(session, post)}
            for item in media.values():
                issue_url(item, attempt.started_at)

            def url(media_id: str) -> str:
                return public_url(self.public_base_url, media[media_id])

            return _Plan(
                attempt_id=attempt.id,
                type=PostType(post.type),
                caption=post.caption,
                items=[
                    (MediaType(media[i.media_id].type).is_video, url(i.media_id))
                    for i in post.items
                ],
                cover_url=url(post.cover_media_id) if post.cover_media_id else None,
                thumb_offset_ms=post.thumb_offset_ms,
            )

    def _new_attempt(
        self, session: Session, post: Post, container_ref: str | None = None
    ) -> PublishAttempt:
        last = session.scalar(
            select(func.max(PublishAttempt.number)).where(PublishAttempt.post_id == post.id)
        )
        attempt = PublishAttempt(
            post_id=post.id,
            number=(last or 0) + 1,
            started_at=self.clock.now(),
            result=AttemptResult.IN_PROGRESS,
            container_ref=container_ref,
        )
        session.add(attempt)
        # Written before any call to Meta, so a crash never leaves an attempt without a trace.
        post.status = PostStatus.PUBLISHING
        session.flush()
        return attempt

    def _attempt(self, sessions: sessionmaker, plan: _Plan) -> None:
        try:
            container = self._create_containers(plan)
        except ContainerFailed as exc:
            self._end(sessions, plan.attempt_id, AttemptResult.REJECTED, failure=exc.failure)
            return
        except Refused as exc:
            self._end(
                sessions, plan.attempt_id, AttemptResult.REJECTED, failure="refused", error=exc
            )
            return
        except InstagramError as exc:
            # media_publish was never called, so nothing can be live.
            self._end(
                sessions,
                plan.attempt_id,
                AttemptResult.SERVER_ERROR,
                failure="unavailable",
                error=exc,
            )
            return
        with sessions() as session, session.begin():
            session.get_one(PublishAttempt, plan.attempt_id).container_ref = container
        self._publish(sessions, plan.attempt_id, container)

    def _create_containers(self, plan: _Plan) -> str:
        caption = plan.caption or None
        is_video, url = plan.items[0]
        match plan.type:
            case PostType.CAROUSEL:
                children = []
                for item_is_video, item_url in plan.items:
                    kind = ContainerKind.VIDEO if item_is_video else ContainerKind.IMAGE
                    child = self.instagram.create_container(
                        ContainerRequest(kind, url=item_url, is_carousel_item=True)
                    )
                    self._wait(child)
                    children.append(child)
                request = ContainerRequest(
                    ContainerKind.CAROUSEL, caption=caption, children=tuple(children)
                )
            case PostType.REEL:
                request = ContainerRequest(
                    ContainerKind.REELS,
                    url=url,
                    caption=caption,
                    cover_url=plan.cover_url,
                    thumb_offset_ms=plan.thumb_offset_ms,
                )
            case PostType.STORY:
                # Stories take no caption; the catalog warned when one was set.
                request = ContainerRequest(ContainerKind.STORIES, url=url, is_video=is_video)
            case PostType.IMAGE:
                request = ContainerRequest(ContainerKind.IMAGE, url=url, caption=caption)
        container = self.instagram.create_container(request)
        self._wait(container)
        return container

    def _wait(self, container: str) -> None:
        deadline = self.clock.now() + POLL_TIMEOUT
        while True:
            status = self.instagram.container_status(container)
            if status == ContainerStatus.FINISHED:
                return
            if status in {ContainerStatus.ERROR, ContainerStatus.EXPIRED}:
                raise ContainerFailed(f"container_{status.lower()}")
            if self.clock.now() >= deadline:
                raise ContainerFailed("container_timeout")
            self.sleep(POLL_INTERVAL.total_seconds())

    def _publish(self, sessions: sessionmaker, attempt_id: str, container: str) -> None:
        try:
            media_ref = self.instagram.publish(container)
        except Refused as exc:
            # A 4xx should mean nothing went live, but only PUBLISHED proves it either way.
            try:
                status = self.instagram.container_status(container)
            except InstagramError:
                status = None
            if status in {None, ContainerStatus.PUBLISHED}:
                self._end(sessions, attempt_id, AttemptResult.UNKNOWN, error=exc)
            else:
                self._end(
                    sessions, attempt_id, AttemptResult.REJECTED, failure="refused", error=exc
                )
            return
        except InstagramError as exc:
            # 5xx or lost connection: it may be live. Never call media_publish again blindly.
            self._end(sessions, attempt_id, AttemptResult.UNKNOWN, error=exc)
            return
        post_id = self._end(sessions, attempt_id, AttemptResult.OK, media_ref=media_ref)
        try:
            permalink = self.instagram.media(media_ref).permalink
        except InstagramError as exc:
            log.warning("published, permalink lookup failed", extra={"error": str(exc)})
            return
        with sessions() as session, session.begin():
            session.get_one(Post, post_id).permalink = permalink

    def _end(
        self,
        sessions: sessionmaker,
        attempt_id: str,
        result: AttemptResult,
        *,
        failure: str | None = None,
        error: InstagramError | None = None,
        media_ref: str | None = None,
    ) -> str:
        now = self.clock.now()
        with sessions() as session, session.begin():
            attempt = session.get_one(PublishAttempt, attempt_id)
            post = session.get_one(Post, attempt.post_id)
            attempt.result = result
            attempt.ended_at = now
            if error is not None:
                attempt.error_code, attempt.error_subcode = error.code, error.subcode
            self._revoke_urls(session, post)
            match result:
                case AttemptResult.OK:
                    self._mark_published(session, post, media_ref, now)
                case AttemptResult.UNKNOWN:
                    post.status = PostStatus.OUTCOME_UNKNOWN
                case _:
                    post.status = PostStatus.FAILED
                    post.failure = failure
        log.info(
            "publish attempt ended",
            extra={"post_id": post.id, "result": str(result), "failure": failure},
        )
        return post.id

    # Unknown outcomes

    def resolve(self, sessions: sessionmaker) -> None:
        now = self.clock.now()
        with sessions() as session, session.begin():
            stale = session.scalars(
                select(PublishAttempt)
                .join(Post)
                .where(
                    Post.network == self.instagram.network,
                    PublishAttempt.result == AttemptResult.IN_PROGRESS,
                    PublishAttempt.started_at < now - STALE_ATTEMPT,
                )
            ).all()
            for attempt in stale:
                # The worker died mid-attempt: from here on it is an unknown outcome like any other.
                attempt.result = AttemptResult.UNKNOWN
                attempt.ended_at = now
                session.get_one(Post, attempt.post_id).status = PostStatus.OUTCOME_UNKNOWN
            session.flush()
            pending = [
                _Pending(a.id, a.container_ref, a.started_at, PostType(p.type), p.caption)
                for a, p in session.execute(
                    select(PublishAttempt, Post)
                    .join(Post)
                    .where(
                        Post.network == self.instagram.network,
                        Post.status == PostStatus.OUTCOME_UNKNOWN,
                        PublishAttempt.result == AttemptResult.UNKNOWN,
                        PublishAttempt.resolved_at.is_(None),
                    )
                ).all()
            ]
        for item in pending:
            self._resolve_one(sessions, item)

    def _resolve_one(self, sessions: sessionmaker, pending: _Pending) -> None:
        if pending.container_ref is None:
            # It died before media_publish could be called: publish it again.
            self._settle(
                sessions, pending.attempt_id, Resolution.NOT_PUBLISHED, PostStatus.SCHEDULED
            )
            return
        try:
            status = self.instagram.container_status(pending.container_ref)
            found = (
                self._find_published(sessions, pending)
                if status == ContainerStatus.PUBLISHED
                else None
            )
        except InstagramError as exc:
            log.warning("could not resolve attempt yet", extra={"error": str(exc)})
            return
        match status:
            case ContainerStatus.FINISHED:
                # Documented as ready and not published, so publishing it now cannot duplicate.
                retry = self._settle(
                    sessions,
                    pending.attempt_id,
                    Resolution.NOT_PUBLISHED,
                    PostStatus.PUBLISHING,
                    retry_container=pending.container_ref,
                )
                if retry is not None:
                    self._publish(sessions, retry, pending.container_ref)
            case ContainerStatus.ERROR | ContainerStatus.EXPIRED:
                self._settle(
                    sessions,
                    pending.attempt_id,
                    Resolution.NOT_PUBLISHED,
                    PostStatus.FAILED,
                    failure=f"container_{status.lower()}",
                )
            case ContainerStatus.PUBLISHED:
                self._settle(
                    sessions, pending.attempt_id, Resolution.PUBLISHED, PostStatus.PUBLISHED, found
                )
            case ContainerStatus.IN_PROGRESS:
                pass

    def _find_published(self, sessions: sessionmaker, pending: _Pending) -> PublishedMedia | None:
        """The live media of a PUBLISHED container, which does not return its id. Stories are
        not listed, so for them this finds nothing."""
        if pending.type == PostType.STORY:
            return None
        product = ProductType.REELS if pending.type == PostType.REEL else ProductType.FEED
        with sessions() as session:
            taken = set(
                session.scalars(
                    select(Post.external_ref).where(
                        Post.network == self.instagram.network, Post.external_ref.is_not(None)
                    )
                )
            )
        candidates = [
            m
            for m in self.instagram.list_media(max_items=RECENT_MEDIA_TO_SEARCH)
            if m.id not in taken
            and m.product_type == product
            and m.timestamp >= pending.started_at - TIMESTAMP_SLACK
            and (m.caption or "").strip() == pending.caption.strip()
        ]
        return min(candidates, key=lambda m: m.timestamp, default=None)

    def _settle(
        self,
        sessions: sessionmaker,
        attempt_id: str,
        resolution: Resolution,
        post_status: PostStatus,
        found: PublishedMedia | None = None,
        *,
        failure: str | None = None,
        retry_container: str | None = None,
    ) -> str | None:
        """Records the resolution; returns the id of the retry attempt when there is one."""
        now = self.clock.now()
        with sessions() as session, session.begin():
            attempt = session.get_one(PublishAttempt, attempt_id)
            post = session.get_one(Post, attempt.post_id)
            if attempt.resolved_at is not None or post.status != PostStatus.OUTCOME_UNKNOWN:
                return None  # settled by hand in the meantime
            attempt.resolved_at = now
            attempt.resolved_as = resolution
            post.status = post_status
            post.failure = failure
            if post_status == PostStatus.SCHEDULED:
                # Due now: its original time may be past the grace window after a crash.
                post.scheduled_for = now
            self._revoke_urls(session, post)
            if post_status == PostStatus.PUBLISHED:
                self._mark_published(
                    session, post, found and found.id, found.timestamp if found else now
                )
            log.info(
                "publish attempt resolved",
                extra={"post_id": post.id, "resolved_as": str(resolution)},
            )
            if retry_container is not None:
                return self._new_attempt(session, post, retry_container).id
        return None

    # Shared

    def _media_of(self, session: Session, post: Post) -> list[Media]:
        return list(session.scalars(select(Media).where(Media.id.in_(post.media_ids))))

    def _revoke_urls(self, session: Session, post: Post) -> None:
        for media in self._media_of(session, post):
            revoke_url(media)

    def _mark_published(
        self, session: Session, post: Post, media_ref: str | None, published_at: datetime
    ) -> None:
        post.status = PostStatus.PUBLISHED
        post.external_ref = media_ref
        post.published_at = published_at
        for media in self._media_of(session, post):
            media.delete_at = self.clock.now() + KEEP_AFTER_PUBLISH

    def purge(self, sessions: sessionmaker) -> None:
        """Deletes files past their delete_at, unless a post still in play uses them."""
        now = self.clock.now()
        in_use = union(
            select(PostItem.media_id).join(Post).where(Post.status.in_(ACTIVE)),
            select(Post.cover_media_id).where(
                Post.status.in_(ACTIVE), Post.cover_media_id.is_not(None)
            ),
        )
        with sessions() as session, session.begin():
            expired = session.scalars(
                select(Media).where(
                    Media.delete_at <= now,
                    Media.deleted_at.is_(None),
                    Media.id.not_in(in_use),
                )
            )
            for media in expired:
                path_of(self.media_dir, media).unlink(missing_ok=True)
                media.deleted_at = now
                revoke_url(media)
