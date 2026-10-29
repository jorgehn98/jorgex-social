from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import func, select

from jorgex_social.access.deps import SessionDep, require
from jorgex_social.access.tokens import Group, Level
from jorgex_social.stats.models import AccountSnapshot, PostSnapshot
from jorgex_social.stats.snapshots import NETWORK

router = APIRouter(
    prefix="/v1/stats", tags=["stats"], dependencies=[Depends(require(Group.STATS, Level.READ))]
)


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


@router.get("/account", response_model=list[AccountSnapshotOut])
def account_history(
    session: SessionDep,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    limit: Limit = 90,
):
    """Account metrics per closed day, newest first."""
    query = select(AccountSnapshot).where(AccountSnapshot.network == NETWORK)
    if since:
        query = query.where(AccountSnapshot.period_start >= since)
    if until:
        query = query.where(AccountSnapshot.period_end <= until)
    return session.scalars(query.order_by(AccountSnapshot.period_start.desc()).limit(limit)).all()


@router.get("/media", response_model=list[PostSnapshotOut])
def latest_per_media(session: SessionDep, limit: Limit = 100):
    """The most recent snapshot of each media, newest publication first."""
    latest = (
        select(PostSnapshot.media_ref, func.max(PostSnapshot.taken_at).label("taken_at"))
        .where(PostSnapshot.network == NETWORK)
        .group_by(PostSnapshot.media_ref)
        .subquery()
    )
    query = (
        select(PostSnapshot)
        .join(
            latest,
            (PostSnapshot.media_ref == latest.c.media_ref)
            & (PostSnapshot.taken_at == latest.c.taken_at),
        )
        .where(PostSnapshot.network == NETWORK)
        .order_by(PostSnapshot.published_at.desc())
        .limit(limit)
    )
    return session.scalars(query).all()


@router.get("/media/{media_ref}", response_model=list[PostSnapshotOut])
def media_history(media_ref: str, session: SessionDep, limit: Limit = 365):
    """Every snapshot of one media, newest first. Empty if none has been taken yet."""
    query = (
        select(PostSnapshot)
        .where(PostSnapshot.network == NETWORK, PostSnapshot.media_ref == media_ref)
        .order_by(PostSnapshot.taken_at.desc())
        .limit(limit)
    )
    return session.scalars(query).all()
