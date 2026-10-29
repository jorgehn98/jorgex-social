from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol


class ContainerKind(StrEnum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"  # only as a carousel item
    REELS = "REELS"
    STORIES = "STORIES"
    CAROUSEL = "CAROUSEL"


class ContainerStatus(StrEnum):
    """Documented values of `status_code` (Content Publishing)."""

    IN_PROGRESS = "IN_PROGRESS"
    FINISHED = "FINISHED"  # ready to publish; not published
    PUBLISHED = "PUBLISHED"
    ERROR = "ERROR"
    EXPIRED = "EXPIRED"  # not published within 24 hours


class ProductType(StrEnum):
    FEED = "FEED"
    REELS = "REELS"
    STORY = "STORY"


@dataclass(frozen=True)
class ContainerRequest:
    kind: ContainerKind
    url: str | None = None  # image_url or video_url, depending on the kind
    is_video: bool = False  # stories only: the url is a video
    caption: str | None = None
    cover_url: str | None = None  # reels only; when set, Instagram ignores thumb_offset
    thumb_offset_ms: int | None = None
    is_carousel_item: bool = False
    children: tuple[str, ...] = field(default_factory=tuple)

    def to_params(self) -> dict[str, Any]:
        match self.kind:
            case ContainerKind.IMAGE:
                params = {"image_url": self.url, "caption": self.caption}
            case ContainerKind.VIDEO:
                params = {"media_type": "VIDEO", "video_url": self.url}
            case ContainerKind.REELS:
                params = {
                    "media_type": "REELS",
                    "video_url": self.url,
                    "caption": self.caption,
                    "cover_url": self.cover_url,
                    "thumb_offset": self.thumb_offset_ms,
                }
            case ContainerKind.STORIES:
                key = "video_url" if self.is_video else "image_url"
                params = {"media_type": "STORIES", key: self.url}
            case ContainerKind.CAROUSEL:
                params = {
                    "media_type": "CAROUSEL",
                    "children": ",".join(self.children),
                    "caption": self.caption,
                }
        if self.is_carousel_item:
            params["is_carousel_item"] = "true"
        return {k: v for k, v in params.items() if v is not None}


@dataclass(frozen=True)
class Account:
    id: str
    username: str
    account_type: str | None
    followers_count: int | None
    media_count: int | None


@dataclass(frozen=True)
class PublishingLimit:
    quota_usage: int
    quota_total: int | None
    quota_duration_seconds: int | None


@dataclass(frozen=True)
class Media:
    id: str
    product_type: ProductType
    media_type: str
    permalink: str | None
    timestamp: datetime
    caption: str | None  # our own text; third-party text never passes through this type


@dataclass(frozen=True)
class Renewal:
    """A renewed access token. The token never shows in repr, so it cannot leak into logs."""

    access_token: str = field(repr=False)
    expires_in: timedelta


class Instagram(Protocol):
    """What the service needs from Instagram so far. It grows with each build step."""

    # Stored with every row, so simulated data can never pass for real data.
    network: str

    def account(self) -> Account: ...
    def publishing_limit(self) -> PublishingLimit: ...
    def create_container(self, request: ContainerRequest) -> str: ...
    def container_status(self, container_id: str) -> ContainerStatus: ...
    def publish(self, container_id: str) -> str: ...
    def media(self, media_id: str) -> Media: ...
    def list_media(self, max_items: int | None = None) -> list[Media]: ...
    def media_insights(self, media_id: str, product_type: ProductType) -> dict[str, Any]: ...
    def account_insights(self, since: datetime, until: datetime) -> dict[str, Any]: ...
    # Meta renews a long-lived token that is at least 24 hours old and has not expired.
    def refresh_token(self) -> Renewal: ...
    # Raises CredentialUnavailable if the stored credential cannot be read; calls nothing remote.
    def check_credential(self) -> None: ...
