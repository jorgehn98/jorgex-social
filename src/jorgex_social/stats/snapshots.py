import logging
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from jorgex_social.clock import Clock
from jorgex_social.instagram.errors import AuthError, InstagramError, RateLimited
from jorgex_social.instagram.types import Instagram, Media, ProductType
from jorgex_social.publishing.models import Post, PostStatus, PostType
from jorgex_social.stats.models import AccountSnapshot, PostSnapshot, RecentMedia

log = logging.getLogger(__name__)

SWEEP_EVERY = timedelta(hours=1)
# Instagram data can lag up to 48 hours, so a day is only recorded once it is that old.
DATA_DELAY = timedelta(hours=48)
# Account insights are kept by Instagram for 90 days; on a first run, recover the last 30.
BACKFILL_DAYS = 30
# Instagram keeps media metrics for up to 2 years; asking for older ones only fails.
MEDIA_INSIGHTS_RETENTION = timedelta(days=730)
# A media whose metrics failed is not asked again for this long.
FAILURE_BACKOFF = timedelta(days=1)
# Recent media change fast; older media are sampled weekly.
RECENT_MEDIA = timedelta(days=30)
RECENT_INTERVAL = timedelta(days=1)
OLD_INTERVAL = timedelta(days=7)
# Stories are not listed by /media and their metrics vanish after 24 hours, so the ones this
# service published are sampled from its own posts table while they last.
STORY_LIFETIME = timedelta(hours=24)
STORY_INTERVAL = timedelta(hours=6)

# Media kept for agents to see what is on the account (GET /v1/instagram-media).
RECENT_MEDIA_KEPT = 25

# The sweep stops on these: retrying item by item would only repeat the failure.
_STOP_SWEEP = (AuthError, RateLimited)


def _day_start(moment: datetime) -> datetime:
    return datetime.combine(moment.date(), time(), tzinfo=moment.tzinfo)


def closed_days(now: datetime) -> list[tuple[datetime, datetime]]:
    """Whole UTC days, oldest first, whose data should be final by now."""
    last_end = _day_start(now - DATA_DELAY)
    return [
        (last_end - timedelta(days=n + 1), last_end - timedelta(days=n))
        for n in reversed(range(BACKFILL_DAYS))
    ]


def store_recent_media(
    sessions: sessionmaker, network: str, items: list[Media], now: datetime
) -> None:
    """Replaces the stored recent media with the newest of `items`, a listing the caller already
    made: storing it costs no call to the network. Best effort: a database failure here is
    logged and must not stop the stats sweep or the resolver that made the listing."""
    newest = sorted(items, key=lambda m: m.timestamp, reverse=True)[:RECENT_MEDIA_KEPT]
    rows = [
        {
            "id": m.id,
            "product_type": m.product_type.value,
            "media_type": m.media_type,
            "permalink": m.permalink,
            "timestamp": m.timestamp.isoformat(),
        }
        for m in newest
    ]
    try:
        with sessions() as session, session.begin():
            session.merge(RecentMedia(network=network, fetched_at=now, items=rows))
    except SQLAlchemyError as exc:
        log.warning(
            "could not store the recent media",
            extra={"network": network, "error": type(exc).__name__},
        )


def snapshot_account(sessions: sessionmaker, instagram: Instagram, now: datetime) -> int:
    with sessions() as session:
        recorded = set(
            session.scalars(
                select(AccountSnapshot.period_start).where(
                    AccountSnapshot.network == instagram.network
                )
            )
        )
    missing = [(start, end) for start, end in closed_days(now) if start not in recorded]
    if not missing:
        return 0
    followers = instagram.account().followers_count
    taken = 0
    for start, end in missing:
        try:
            metrics = instagram.account_insights(start, end)
        except _STOP_SWEEP:
            raise
        except InstagramError as exc:
            log.warning("account insights failed", extra={"day": start.date(), "error": str(exc)})
            continue
        with sessions() as session, session.begin():
            session.add(
                AccountSnapshot(
                    network=instagram.network,
                    period_start=start,
                    period_end=end,
                    taken_at=now,
                    followers=followers,
                    metrics=metrics,
                )
            )
        taken += 1
    return taken


def _due(media: Media, last: datetime | None, now: datetime) -> bool:
    if now - media.timestamp > MEDIA_INSIGHTS_RETENTION:
        return False
    if last is None:
        return True
    interval = RECENT_INTERVAL if now - media.timestamp < RECENT_MEDIA else OLD_INTERVAL
    return now - last >= interval


def snapshot_media(
    sessions: sessionmaker,
    instagram: Instagram,
    now: datetime,
    failed_at: dict[str, datetime] | None = None,
) -> int:
    """Feed posts and reels, including the ones published by hand."""
    items = instagram.list_media()
    store_recent_media(sessions, instagram.network, items, now)
    with sessions() as session:
        last_taken = dict(
            session.execute(
                select(PostSnapshot.media_ref, func.max(PostSnapshot.taken_at))
                .where(PostSnapshot.network == instagram.network)
                .group_by(PostSnapshot.media_ref)
            ).all()
        )
    taken = 0
    for media in items:
        if not _due(media, last_taken.get(media.id), now):
            continue
        if (
            failed_at is not None
            and now - failed_at.get(media.id, now - FAILURE_BACKOFF) < FAILURE_BACKOFF
        ):
            continue
        try:
            metrics = instagram.media_insights(media.id, media.product_type)
        except _STOP_SWEEP:
            raise
        except InstagramError as exc:
            # One deleted post or failing call must not hide the rest.
            log.warning("media insights failed", extra={"media_ref": media.id, "error": str(exc)})
            if failed_at is not None:
                failed_at[media.id] = now
            continue
        with sessions() as session, session.begin():
            session.add(
                PostSnapshot(
                    network=instagram.network,
                    media_ref=media.id,
                    taken_at=now,
                    product_type=media.product_type.value,
                    published_at=media.timestamp,
                    permalink=media.permalink,
                    metrics=metrics,
                )
            )
        taken += 1
    return taken


def snapshot_stories(sessions: sessionmaker, instagram: Instagram, now: datetime) -> int:
    """Stories published by this service, every 6 hours during their first 24. A failed one is
    retried on the next hourly sweep: a day of backoff would outlive the story."""
    with sessions() as session:
        stories = session.execute(
            select(Post.external_ref, Post.published_at, Post.permalink).where(
                Post.network == instagram.network,
                Post.type == PostType.STORY,
                Post.status == PostStatus.PUBLISHED,
                Post.external_ref.is_not(None),
                Post.published_at > now - STORY_LIFETIME,
            )
        ).all()
        last_taken = dict(
            session.execute(
                select(PostSnapshot.media_ref, func.max(PostSnapshot.taken_at))
                .where(
                    PostSnapshot.network == instagram.network,
                    PostSnapshot.media_ref.in_([ref for ref, _, _ in stories]),
                )
                .group_by(PostSnapshot.media_ref)
            ).all()
        )
    taken = 0
    for media_ref, published_at, permalink in stories:
        last = last_taken.get(media_ref)
        if last is not None and now - last < STORY_INTERVAL:
            continue
        try:
            metrics = instagram.media_insights(media_ref, ProductType.STORY)
        except _STOP_SWEEP:
            raise
        except InstagramError as exc:
            log.warning("story insights failed", extra={"media_ref": media_ref, "error": str(exc)})
            continue
        with sessions() as session, session.begin():
            session.add(
                PostSnapshot(
                    network=instagram.network,
                    media_ref=media_ref,
                    taken_at=now,
                    product_type=ProductType.STORY.value,
                    published_at=published_at,
                    permalink=permalink,
                    metrics=metrics,
                )
            )
        taken += 1
    return taken


class SnapshotTask:
    """Worker task: at most one sweep per hour; each sweep only fetches what is due."""

    __name__ = "snapshots"

    def __init__(self, instagram: Instagram, clock: Clock) -> None:
        self.instagram = instagram
        self.clock = clock
        self._last_sweep: datetime | None = None
        self._failed_at: dict[str, datetime] = {}

    def __call__(self, sessions: sessionmaker) -> None:
        now = self.clock.now()
        if self._last_sweep and now - self._last_sweep < SWEEP_EVERY:
            return
        self._last_sweep = now
        steps = (
            ("account", lambda: snapshot_account(sessions, self.instagram, now)),
            ("media", lambda: snapshot_media(sessions, self.instagram, now, self._failed_at)),
            ("stories", lambda: snapshot_stories(sessions, self.instagram, now)),
        )
        for name, step in steps:
            try:
                taken = step()
            except _STOP_SWEEP:
                raise
            except InstagramError as exc:
                log.warning(
                    "snapshot step failed", extra={"step": step.__name__, "error": str(exc)}
                )
                continue
            if taken:
                log.info("snapshots taken", extra={"step": name, "count": taken})
