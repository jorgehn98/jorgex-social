from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from jorgex_social.db import Base, UTCDateTime


class PostSnapshot(Base):
    """Metrics of one published media at one moment. Never overwritten."""

    __tablename__ = "post_snapshots"

    network: Mapped[str] = mapped_column(String(32), primary_key=True)
    media_ref: Mapped[str] = mapped_column(String(64), primary_key=True)
    taken_at: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    product_type: Mapped[str] = mapped_column(String(16))
    published_at: Mapped[datetime] = mapped_column(UTCDateTime)
    permalink: Mapped[str | None] = mapped_column(String(500))
    # A metric the network did not return is absent, never zero.
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON)


class AccountSnapshot(Base):
    """Account metrics for one closed period, plus the follower count when it was taken."""

    __tablename__ = "account_snapshots"
    network: Mapped[str] = mapped_column(String(32), primary_key=True)
    period_start: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    period_end: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    taken_at: Mapped[datetime] = mapped_column(UTCDateTime)
    followers: Mapped[int | None] = mapped_column(Integer)  # at taken_at, not over the period
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON)
