from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import count
from typing import Any

from jorgex_social.clock import Clock
from jorgex_social.instagram.client import check_insights_window
from jorgex_social.instagram.errors import InstagramError, Refused
from jorgex_social.instagram.types import (
    Account,
    ContainerKind,
    ContainerRequest,
    ContainerStatus,
    Media,
    ProductType,
    PublishingLimit,
)

# Documented limits (docs/research/references/instagram.md); the fake enforces them so the
# service meets them in simulation before it meets them in production.
CONTAINER_LIFETIME = timedelta(hours=24)
STORY_INSIGHTS_LIFETIME = timedelta(hours=24)
MAX_CAROUSEL_ITEMS = 20
PUBLISHING_WINDOW = timedelta(hours=24)

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
class _Failure:
    error: InstagramError
    after: bool  # True: the call takes effect and then fails, like a 5xx after going live


class FakeInstagram:
    """Stateful, in-memory Instagram with a controllable clock. All data is synthetic.

    Video containers take `video_processing` to become FINISHED; images are ready at once.
    `fail_next` injects errors, including the ones that leave a write's outcome unknown.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        video_processing: timedelta = timedelta(seconds=30),
        quota_total: int = 100,
    ) -> None:
        self.clock = clock
        self.video_processing = video_processing
        self.quota_total = quota_total
        self._ids = count(17_000_000_000_000_001)
        self._containers: dict[str, _Container] = {}
        self._media: dict[str, tuple[Media, ContainerKind]] = {}
        self._failures: dict[str, list[_Failure]] = {}

    # Test and simulation controls

    def fail_next(self, operation: str, error: InstagramError, *, after: bool = False) -> None:
        self._failures.setdefault(operation, []).append(_Failure(error, after))

    def mark_error(self, container_id: str) -> None:
        self._containers[container_id].error = True

    def _failure(self, operation: str) -> _Failure | None:
        queue = self._failures.get(operation)
        return queue.pop(0) if queue else None

    def _run(self, operation: str, action):
        failure = self._failure(operation)
        if failure and not failure.after:
            raise failure.error
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
                id="17841400000000000",
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
