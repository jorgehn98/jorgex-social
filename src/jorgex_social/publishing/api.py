import hashlib
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from jorgex_social.access.deps import SessionDep, require, require_outside_transaction
from jorgex_social.access.tokens import Group, Level
from jorgex_social.publishing import catalog
from jorgex_social.publishing import media as storage
from jorgex_social.publishing.catalog import Check, Issue
from jorgex_social.publishing.models import (
    AttemptResult,
    Media,
    Post,
    PostItem,
    PostStatus,
    PostType,
    Resolution,
)

router = APIRouter(prefix="/v1", tags=["publishing"])
# Instagram downloads media from here, so it takes no token: the unguessable, expiring URL token
# is the credential.
public_router = APIRouter()

PostsRead = [Depends(require(Group.POSTS, Level.READ))]
PostsWrite = [Depends(require(Group.POSTS, Level.WRITE))]
SchedulingWrite = [Depends(require(Group.SCHEDULING, Level.WRITE))]

EDITABLE = {PostStatus.DRAFT, PostStatus.SCHEDULED}
SCHEDULABLE = {PostStatus.DRAFT, PostStatus.SCHEDULED, PostStatus.FAILED}


def _error(status_code: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code, detail={"code": code, "message": message, **extra})


# Media


class MediaOut(BaseModel):
    id: str
    type: str
    sha256: str
    bytes: int
    created_at: datetime


def _save_media(sessions: sessionmaker, media: Media) -> None:
    with sessions() as session, session.begin():
        session.add(media)


@router.post(
    "/media",
    status_code=status.HTTP_201_CREATED,
    response_model=MediaOut,
    dependencies=[Depends(require_outside_transaction(Group.POSTS, Level.WRITE))],
)
async def upload_media(request: Request):
    """Upload one file as the raw request body (for example `curl --data-binary @file`), with
    Content-Type image/jpeg, video/mp4 or video/quicktime. The stored type comes from the file
    signature, not from the header."""
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type not in storage.UPLOAD_CONTENT_TYPES:
        raise _error(
            415,
            "unsupported_media_type",
            f"Content-Type must be one of {', '.join(sorted(storage.UPLOAD_CONTENT_TYPES))}.",
        )
    limit = storage.MAX_UPLOAD_BYTES
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise _error(413, "too_large", f"Uploads are limited to {limit} bytes.")

    media_dir = request.app.state.settings.media_dir
    media_dir.mkdir(parents=True, exist_ok=True)
    media_id = str(uuid.uuid7())
    partial = media_dir / f".{media_id}.part"
    digest, size, head = hashlib.sha256(), 0, b""
    try:
        # Streamed to disk: a 300 MB video never sits in memory.
        with partial.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise _error(413, "too_large", f"Uploads are limited to {limit} bytes.")
                if len(head) < storage.SNIFF_BYTES:
                    head += chunk[: storage.SNIFF_BYTES - len(head)]
                digest.update(chunk)
                fh.write(chunk)
        media_type = storage.sniff(head)
        if media_type is None:
            raise _error(415, "unsupported_media_type", "The file is not a JPEG, MP4 or MOV.")
        final = media_dir / f"{media_id}.{media_type.value}"
        partial.rename(final)
    finally:
        partial.unlink(missing_ok=True)

    media = Media(
        id=media_id,
        relative_path=final.name,
        sha256=digest.hexdigest(),
        type=media_type.value,
        bytes=size,
    )
    try:
        await run_in_threadpool(_save_media, request.app.state.sessionmaker, media)
    except BaseException:
        final.unlink(missing_ok=True)
        raise
    return media


@public_router.api_route("/m/{url_token}", methods=["GET", "HEAD"], include_in_schema=False)
def media_file(url_token: str, request: Request, session: SessionDep):
    media = session.scalar(select(Media).where(Media.url_token == url_token))
    now = request.app.state.clock.now()
    if media is None or media.deleted_at or not media.url_expires_at or media.url_expires_at <= now:
        raise HTTPException(404)
    path = storage.path_of(request.app.state.settings.media_dir, media)
    return FileResponse(path, media_type=storage.CONTENT_TYPE[media.type])


# Catalog


@router.get("/catalog", dependencies=PostsRead)
def get_catalog() -> dict:
    """Limits per post type, as the server enforces them."""
    return {
        "post_types": {t.value: rules for t, rules in catalog.CATALOG.items()},
        "caption": catalog.CAPTION_LIMITS,
        "upload": {
            "content_types": sorted(storage.UPLOAD_CONTENT_TYPES),
            "max_bytes": storage.MAX_UPLOAD_BYTES,
        },
    }


# Posts


class PostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: PostType
    caption: str = Field("", max_length=10_000)
    items: list[str] = Field(max_length=50)
    cover_media_id: str | None = None
    thumb_offset_ms: int | None = None
    source_ref: str | None = Field(None, max_length=500)
    # Sending the same key again returns the post created the first time.
    idempotency_key: str | None = Field(None, min_length=1, max_length=200)


class PostPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: PostType | None = None
    caption: str | None = Field(None, max_length=10_000)
    items: list[str] | None = Field(None, max_length=50)
    cover_media_id: str | None = None
    thumb_offset_ms: int | None = None
    source_ref: str | None = Field(None, max_length=500)


class AttemptOut(BaseModel):
    number: int
    started_at: datetime
    ended_at: datetime | None
    result: str
    container_ref: str | None
    error_code: int | None
    error_subcode: int | None
    resolved_at: datetime | None
    resolved_as: str | None


class PostOut(BaseModel):
    id: str
    network: str
    type: str
    status: str
    caption: str
    items: list[str]
    cover_media_id: str | None
    thumb_offset_ms: int | None
    source_ref: str | None
    idempotency_key: str | None
    content_hash: str
    scheduled_for: datetime | None
    published_at: datetime | None
    external_ref: str | None
    permalink: str | None
    failure: str | None
    created_at: datetime
    updated_at: datetime
    errors: list[Issue]
    warnings: list[Issue]


class PostDetail(PostOut):
    attempts: list[AttemptOut]


def _media(session: Session, media_ids: list[str]) -> dict[str, Media]:
    found = {m.id: m for m in session.scalars(select(Media).where(Media.id.in_(media_ids)))}
    missing = sorted(set(media_ids) - set(found))
    if missing:
        raise _error(422, "unknown_media", "Unknown media ids.", media_ids=missing)
    return found


def _check(session: Session, post: Post) -> Check:
    media = _media(session, post.media_ids)
    return catalog.validate(
        PostType(post.type),
        post.caption,
        [media[item.media_id] for item in post.items],
        media.get(post.cover_media_id) if post.cover_media_id else None,
        post.thumb_offset_ms,
    )


def _out(session: Session, post: Post, *, detail: bool = False) -> dict:
    check = _check(session, post)
    out = {
        **{field: getattr(post, field) for field in PostOut.model_fields if hasattr(post, field)},
        "items": [item.media_id for item in post.items],
        "errors": check.errors,
        "warnings": check.warnings,
    }
    if detail:
        out["attempts"] = post.attempts
    return out


def _set_content(
    session: Session,
    post: Post,
    post_type: PostType,
    caption: str,
    item_ids: list[str],
    cover_id: str | None,
    thumb_offset_ms: int | None,
) -> None:
    media = _media(session, item_ids + ([cover_id] if cover_id else []))
    post.type = post_type
    post.caption = caption
    post.cover_media_id = cover_id
    post.thumb_offset_ms = thumb_offset_ms
    post.content_hash = catalog.content_hash(
        post_type,
        caption,
        [media[i] for i in item_ids],
        media.get(cover_id) if cover_id else None,
        thumb_offset_ms,
    )
    session.add(post)
    # Flush the removal first: the new items reuse the (post_id, position) keys.
    post.items.clear()
    session.flush()
    post.items = [PostItem(position=i, media_id=media_id) for i, media_id in enumerate(item_ids)]


def _network(request: Request) -> str:
    # Simulated posts never show in real mode, and the other way round.
    return request.app.state.network


Network = Annotated[str, Depends(_network)]


def _post(session: Session, network: str, post_id: str) -> Post:
    post = session.get(Post, post_id)
    if post is None or post.network != network:
        raise _error(404, "not_found", "No such post.")
    return post


def _require_status(post: Post, allowed: set[PostStatus], action: str) -> None:
    if post.status not in allowed:
        raise _error(
            409,
            "invalid_state",
            f"Cannot {action} a post that is {post.status}.",
            status=post.status,
            allowed=sorted(allowed),
        )


@router.post(
    "/posts",
    status_code=status.HTTP_201_CREATED,
    response_model=PostOut,
    dependencies=PostsWrite,
)
def create_post(body: PostIn, session: SessionDep, network: Network, response: Response):
    """Create a draft. The response carries its content_hash and the catalog errors and
    warnings; a draft with errors is kept but cannot be scheduled."""
    if body.idempotency_key:
        existing = session.scalar(select(Post).where(Post.idempotency_key == body.idempotency_key))
        if existing is not None:
            if existing.network != network:
                raise _error(409, "idempotency_key_taken", "The key was used in another mode.")
            response.status_code = status.HTTP_200_OK
            return _out(session, existing)
    post = Post(
        network=network,
        source_ref=body.source_ref,
        idempotency_key=body.idempotency_key,
        status=PostStatus.DRAFT,
    )
    _set_content(
        session,
        post,
        body.type,
        body.caption,
        body.items,
        body.cover_media_id,
        body.thumb_offset_ms,
    )
    return _out(session, post)


@router.get("/posts", response_model=list[PostOut], dependencies=PostsRead)
def list_posts(
    session: SessionDep,
    network: Network,
    status: PostStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Posts, newest first."""
    query = select(Post).where(Post.network == network)
    if status:
        query = query.where(Post.status == status)
    posts = session.scalars(query.order_by(Post.created_at.desc()).limit(limit).offset(offset))
    return [_out(session, post) for post in posts]


@router.get("/posts/{post_id}", response_model=PostDetail, dependencies=PostsRead)
def get_post(post_id: str, session: SessionDep, network: Network):
    """One post with its publish attempts."""
    return _out(session, _post(session, network, post_id), detail=True)


@router.patch("/posts/{post_id}", response_model=PostOut, dependencies=PostsWrite)
def update_post(post_id: str, body: PostPatch, session: SessionDep, network: Network):
    """Edit a draft or a scheduled post. A scheduled post goes back to draft: what changed has
    not been checked by whoever scheduled it."""
    post = _post(session, network, post_id)
    _require_status(post, EDITABLE, "edit")
    fields = body.model_fields_set
    for required in ("type", "caption", "items"):
        if required in fields and getattr(body, required) is None:
            raise _error(422, "invalid_value", f"{required} cannot be null.")
    _set_content(
        session,
        post,
        body.type if "type" in fields else PostType(post.type),
        body.caption if "caption" in fields else post.caption,
        body.items if "items" in fields else [item.media_id for item in post.items],
        body.cover_media_id if "cover_media_id" in fields else post.cover_media_id,
        body.thumb_offset_ms if "thumb_offset_ms" in fields else post.thumb_offset_ms,
    )
    if "source_ref" in fields:
        post.source_ref = body.source_ref
    post.status = PostStatus.DRAFT
    post.scheduled_for = None
    return _out(session, post)


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_for: AwareDatetime
    # The content_hash the caller reviewed; refused with 409 if the post changed since.
    expected_hash: str | None = None


class PublishNowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_hash: str | None = None


def _schedule(
    request: Request, session: Session, post: Post, when: datetime, expected_hash: str | None
) -> dict:
    _require_status(post, SCHEDULABLE, "schedule")
    if expected_hash is not None and expected_hash != post.content_hash:
        raise _error(
            409,
            "content_changed",
            "The post changed since that hash was computed.",
            content_hash=post.content_hash,
        )
    check = _check(session, post)
    if check.errors:
        raise _error(
            422,
            "invalid_post",
            "The post does not pass the catalog.",
            errors=[{"code": e.code, "message": e.message} for e in check.errors],
        )
    settings = request.app.state.settings
    if not settings.simulation and not settings.public_url:
        raise _error(
            409,
            "public_url_missing",
            "Set JORGEX_SOCIAL_PUBLIC_URL: Instagram downloads media from it.",
        )
    post.status = PostStatus.SCHEDULED
    post.scheduled_for = when.astimezone(UTC)
    post.failure = None
    return _out(session, post)


@router.post("/posts/{post_id}/schedule", response_model=PostOut, dependencies=SchedulingWrite)
def schedule_post(
    post_id: str, body: ScheduleIn, request: Request, session: SessionDep, network: Network
):
    """Publish at `scheduled_for` (ISO 8601 with offset). Rescheduling a scheduled or failed post
    is allowed."""
    if body.scheduled_for < request.app.state.clock.now():
        raise _error(422, "in_the_past", "scheduled_for is in the past; use publish-now.")
    post = _post(session, network, post_id)
    return _schedule(request, session, post, body.scheduled_for, body.expected_hash)


@router.post("/posts/{post_id}/publish-now", response_model=PostOut, dependencies=SchedulingWrite)
def publish_now(
    post_id: str, body: PublishNowIn, request: Request, session: SessionDep, network: Network
):
    """Schedule for now; the worker picks it up on its next pass."""
    post = _post(session, network, post_id)
    return _schedule(request, session, post, request.app.state.clock.now(), body.expected_hash)


@router.post("/posts/{post_id}/cancel", response_model=PostOut, dependencies=SchedulingWrite)
def cancel_post(post_id: str, session: SessionDep, network: Network):
    post = _post(session, network, post_id)
    _require_status(post, EDITABLE, "cancel")
    post.status = PostStatus.CANCELLED
    post.scheduled_for = None
    return _out(session, post)


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["published", "not_published"]
    media_ref: str | None = Field(None, max_length=64)


@router.post("/posts/{post_id}/resolve", response_model=PostDetail, dependencies=SchedulingWrite)
def resolve_post(
    post_id: str, body: ResolveIn, request: Request, session: SessionDep, network: Network
):
    """Settle a post whose outcome the service could not determine, after checking the account.
    `not_published` returns it to draft, to be scheduled again on purpose."""
    post = _post(session, network, post_id)
    _require_status(post, {PostStatus.OUTCOME_UNKNOWN}, "resolve")
    now = request.app.state.clock.now()
    if body.outcome == "not_published":
        if body.media_ref:
            raise _error(422, "invalid_value", "media_ref only goes with published.")
        post.status = PostStatus.DRAFT
        post.scheduled_for = None
    else:
        if body.media_ref:
            taken = session.scalar(
                select(Post.id).where(
                    Post.network == network, Post.external_ref == body.media_ref, Post.id != post.id
                )
            )
            if taken:
                raise _error(409, "media_ref_taken", "Another post has that media_ref.")
        post.status = PostStatus.PUBLISHED
        post.external_ref = body.media_ref
        post.published_at = now
        for media in _media(session, post.media_ids).values():
            media.delete_at = now + storage.KEEP_AFTER_PUBLISH
    for attempt in post.attempts:
        if attempt.result == AttemptResult.UNKNOWN and attempt.resolved_at is None:
            attempt.resolved_at = now
            attempt.resolved_as = Resolution.MANUAL
    return _out(session, post, detail=True)
