from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import func, select

from jorgex_social.access.deps import AccountIdDep, SessionDep, require
from jorgex_social.access.tokens import Group, Level
from jorgex_social.stats.models import AccountSnapshot, PostSnapshot, RecentMedia

StatsRead = [Depends(require(Group.STATS, Level.READ))]
router = APIRouter(prefix="/v1/stats", tags=["stats"], dependencies=StatsRead)
# Not a statistic, but the same kind of data (published media ids and permalinks), so the same
# group reads it.
recent_media_router = APIRouter(prefix="/v1", tags=["stats"], dependencies=StatsRead)


class AccountSnapshotOut(BaseModel):
    period_start: datetime
    period_end: datetime
    taken_at: datetime
    followers: int | None
    metrics: dict[str, Any]


class PostSnapshotOut(BaseModel):
    media_ref: str
    product_type: str
    published_at: datetime
    permalink: str | None
    taken_at: datetime
    metrics: dict[str, Any]


Limit = Annotated[int, Query(ge=1, le=1000)]
Offset = Annotated[int, Query(ge=0)]


# The account of the call: the token's or its OAuth grant's. The web interface passes the one
# account of the mode, which it still resolves itself.
Account = AccountIdDep


@router.get("/account", response_model=list[AccountSnapshotOut])
def account_history(
    session: SessionDep,
    account: Account,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    limit: Limit = 90,
):
    """Account metrics per closed day, newest first."""
    query = select(AccountSnapshot).where(AccountSnapshot.account_id == account)
    if since:
        query = query.where(AccountSnapshot.period_start >= since)
    if until:
        query = query.where(AccountSnapshot.period_end <= until)
    return session.scalars(query.order_by(AccountSnapshot.period_start.desc()).limit(limit)).all()


@router.get("/media", response_model=list[PostSnapshotOut])
def latest_per_media(session: SessionDep, account: Account, limit: Limit = 100, offset: Offset = 0):
    """The most recent snapshot of each media, newest publication first."""
    latest = (
        select(PostSnapshot.media_ref, func.max(PostSnapshot.taken_at).label("taken_at"))
        .where(PostSnapshot.account_id == account)
        .group_by(PostSnapshot.media_ref)
        .subquery()
    )
    query = (
        select(PostSnapshot)
        .join(
            latest,
            (PostSnapshot.account_id == account)
            & (PostSnapshot.media_ref == latest.c.media_ref)
            & (PostSnapshot.taken_at == latest.c.taken_at),
        )
        .where(PostSnapshot.account_id == account)
        .order_by(PostSnapshot.published_at.desc(), PostSnapshot.media_ref)
        .limit(limit)
        .offset(offset)
    )
    return session.scalars(query).all()


@router.get("/media/{media_ref}", response_model=list[PostSnapshotOut])
def media_history(media_ref: str, session: SessionDep, account: Account, limit: Limit = 365):
    """Every snapshot of one media, newest first. Empty if none has been taken yet."""
    query = (
        select(PostSnapshot)
        .where(PostSnapshot.account_id == account, PostSnapshot.media_ref == media_ref)
        .order_by(PostSnapshot.taken_at.desc())
        .limit(limit)
    )
    return session.scalars(query).all()


class RecentMediaItem(BaseModel):
    id: str
    product_type: str
    media_type: str
    permalink: str | None
    timestamp: datetime


class RecentMediaOut(BaseModel):
    # When the worker last listed the account; null if it never has.
    fetched_at: datetime | None
    items: list[RecentMediaItem]


@recent_media_router.get("/instagram-media", response_model=RecentMediaOut)
def recent_media(session: SessionDep, account: Account):
    """The account's newest feed posts and reels, newest first, as of the worker's last listing.
    It is refreshed by the hourly statistics sweep (and when the worker finds a published
    container, in which case it settles the post itself). Stories are never listed."""
    row = session.get(RecentMedia, account) if account else None
    if row is None:
        return RecentMediaOut(fetched_at=None, items=[])
    return RecentMediaOut(fetched_at=row.fetched_at, items=row.items)
