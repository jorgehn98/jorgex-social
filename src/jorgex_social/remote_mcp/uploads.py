"""One-use upload URLs, so a remote agent can send a file from its own machine without a token
in sight: the MCP tool asks for a URL, the agent sends the file to it with curl. The URL is the
only credential: no Bearer, no cookie. Its token travels in the path, which the access log
redacts (logs.py)."""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from jorgex_social.access.deps import Principal, require
from jorgex_social.access.limits import client_key
from jorgex_social.access.tokens import Group, Level, hash_token
from jorgex_social.account.audit import set_actor
from jorgex_social.oauth import store as oauth
from jorgex_social.oauth.models import OAuthGrant
from jorgex_social.publishing import media as storage
from jorgex_social.publishing.api import MediaOut, save_upload
from jorgex_social.remote_mcp.models import UploadUrl

log = logging.getLogger(__name__)

LIFETIME = timedelta(minutes=10)
# Per grant: URLs issued and not yet used, and URLs issued in the last hour.
MAX_PENDING = 5
MAX_PER_HOUR = 20
UPLOAD_PATH = "/upload"

router = APIRouter()


class TooMany(Exception):
    pass


def issue(session: Session, grant_id: str, now: datetime) -> str:
    session.execute(delete(UploadUrl).where(UploadUrl.created_at <= now - timedelta(hours=1)))
    recent = session.scalars(select(UploadUrl).where(UploadUrl.grant_id == grant_id)).all()
    pending = [u for u in recent if u.used_at is None and u.expires_at > now]
    if len(pending) >= MAX_PENDING or len(recent) >= MAX_PER_HOUR:
        raise TooMany
    token = secrets.token_urlsafe(32)
    session.add(
        UploadUrl(
            hash=hash_token(token), grant_id=grant_id, created_at=now, expires_at=now + LIFETIME
        )
    )
    return token


def _usable(session: Session, token: str, now: datetime) -> UploadUrl | None:
    found = session.get(UploadUrl, hash_token(token))
    if found is None or found.used_at is not None or found.expires_at <= now:
        return None
    # Revoking the grant or reaching its end kills its URLs. Its permissions never change, and
    # issuing one already needed posts:write.
    grant = session.get(OAuthGrant, found.grant_id)
    if grant is None or grant.revoked_at is not None or oauth.grant_end(grant) <= now:
        return None
    return found


def spend(state, token: str) -> str | None:
    """Marks the URL used before a byte is written, so it never takes two uploads, and returns
    its grant. Looked up first in a read transaction: a made-up token never takes the write
    lock."""
    with state.read_sessionmaker() as session, session.begin():
        if _usable(session, token, state.clock.now()) is None:
            return None
    with state.sessionmaker() as session, session.begin():
        found = _usable(session, token, state.clock.now())
        if found is None:
            return None
        found.used_at = state.clock.now()
        return found.grant_id


@router.post("/v1/upload-urls", status_code=status.HTTP_201_CREATED, tags=["publishing"])
def create_upload_url(
    request: Request, token: Annotated[Principal, Depends(require(Group.POSTS, Level.WRITE))]
) -> dict:
    """A one-use URL, valid ten minutes, to upload one file as the raw body of a POST. For OAuth
    clients (the remote MCP); API tokens upload to POST /v1/media directly."""
    if not isinstance(token, oauth.Access):
        raise HTTPException(
            400,
            detail={
                "code": "oauth_only",
                "message": "Upload URLs are for OAuth clients; upload to POST /v1/media instead.",
            },
        )
    state = request.app.state
    now = state.clock.now()
    try:
        with state.sessionmaker() as session, session.begin():
            secret = issue(session, token.grant_id, now)
    except TooMany:
        raise HTTPException(
            429,
            detail={
                "code": "too_many_upload_urls",
                "message": f"At most {MAX_PENDING} unused upload URLs and {MAX_PER_HOUR} an hour.",
            },
        ) from None
    return {
        "url": f"{state.settings.public_url}{UPLOAD_PATH}/{secret}",
        "expires_at": (now + LIFETIME).isoformat(),
        "max_bytes": storage.MAX_UPLOAD_BYTES,
    }


@router.post(
    UPLOAD_PATH + "/{upload_token}",
    status_code=status.HTTP_201_CREATED,
    response_model=MediaOut,
    include_in_schema=False,
)
async def upload(upload_token: str, request: Request):
    """The file as the raw body (`curl --data-binary @file <url>`); its type comes from its
    signature, so no Content-Type is needed."""
    state = request.app.state
    client = client_key(request.client.host if request.client else None)
    if state.oauth_limiter.hit(client, state.clock.now()) is not None:
        raise HTTPException(429)
    grant_id = await run_in_threadpool(spend, state, upload_token)
    if grant_id is None:
        raise HTTPException(404)
    set_actor(request, f"oauth:{grant_id}")  # audited under the grant, like its API calls
    return await save_upload(request)
