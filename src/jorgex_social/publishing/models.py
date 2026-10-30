import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jorgex_social.db import Base, UTCDateTime, utcnow


def _uuid() -> str:
    return str(uuid.uuid7())


class MediaType(StrEnum):
    JPEG = "jpeg"
    MP4 = "mp4"
    MOV = "mov"

    @property
    def is_video(self) -> bool:
        return self is not MediaType.JPEG


class PostType(StrEnum):
    IMAGE = "image"
    REEL = "reel"
    CAROUSEL = "carousel"
    STORY = "story"


class PostStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCELLED = "cancelled"


class AttemptResult(StrEnum):
    IN_PROGRESS = "in_progress"
    OK = "ok"
    REJECTED = "rejected"  # nothing went live
    UNKNOWN = "unknown"  # it may have gone live; only the resolver or a human decides
    SERVER_ERROR = "server_error"  # 5xx before media_publish: nothing went live


class Resolution(StrEnum):
    PUBLISHED = "published"
    NOT_PUBLISHED = "not_published"
    MANUAL = "manual"


class Media(Base):
    """An uploaded file. Immutable once stored: its sha256 is part of every post hash."""

    __tablename__ = "media"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    relative_path: Mapped[str] = mapped_column(String(200))  # inside the media directory
    sha256: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(8))
    bytes: Mapped[int] = mapped_column(Integer)
    # Instagram downloads the file from /m/{url_token} while a publish attempt needs it.
    url_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    url_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delete_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("network", "external_ref"),
        Index("ix_posts_status_scheduled_for", "status", "scheduled_for"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    network: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=PostStatus.DRAFT)
    caption: Mapped[str] = mapped_column(Text, default="")
    cover_media_id: Mapped[str | None] = mapped_column(ForeignKey("media.id"))
    thumb_offset_ms: Mapped[int | None] = mapped_column(Integer)
    scheduled_for: Mapped[datetime | None] = mapped_column(UTCDateTime)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    external_ref: Mapped[str | None] = mapped_column(String(64))
    permalink: Mapped[str | None] = mapped_column(String(500))
    # Why the post is `failed`: missed_schedule, daily_limit, refused, container_error, ...
    failure: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    source_ref: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    items: Mapped[list[PostItem]] = relationship(
        order_by="PostItem.position", cascade="all, delete-orphan"
    )
    attempts: Mapped[list[PublishAttempt]] = relationship(order_by="PublishAttempt.number")

    @property
    def media_ids(self) -> list[str]:
        """Every media the post uses, cover included."""
        return [item.media_id for item in self.items] + (
            [self.cover_media_id] if self.cover_media_id else []
        )


class PostItem(Base):
    __tablename__ = "post_items"

    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_id: Mapped[str] = mapped_column(ForeignKey("media.id"))


class PublishAttempt(Base):
    __tablename__ = "publish_attempts"
    __table_args__ = (UniqueConstraint("post_id", "number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"))
    number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    result: Mapped[str] = mapped_column(String(16), default=AttemptResult.IN_PROGRESS)
    # Saved before media_publish: it is what lets an unknown outcome be resolved without guessing.
    container_ref: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[int | None] = mapped_column(Integer)
    error_subcode: Mapped[int | None] = mapped_column(Integer)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_as: Mapped[str | None] = mapped_column(String(16))
