"""OAuth clients, pending authorizations, codes, grants and tokens. Codes, tokens and the values
that tie a consent page to its browser are stored only as hashes (access/tokens.py:hash_token)."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from jorgex_social.db import Base, UTCDateTime


class OAuthClient(Base):
    """A client registered through dynamic registration (RFC 7591). Public clients only."""

    __tablename__ = "oauth_clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # What the client says it is called; shown on the consent page as unverified.
    name: Mapped[str | None] = mapped_column(String(100))
    # The registration record handed back to the SDK: client metadata only, never a secret.
    info: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # Set the first time the operator approves it. Until then it is forgotten after an hour.
    consented_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class OAuthRequest(Base):
    """An authorization request waiting for the operator's decision on the consent page."""

    __tablename__ = "oauth_requests"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("oauth_clients.id"), index=True)
    # The SDK's AuthorizationParams: redirect URI, PKCE challenge, state and resource.
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    # The browser session that first opened the consent page; no other may decide.
    session_hash: Mapped[str | None] = mapped_column(String(64))
    # The one-use value of the consent form last shown.
    nonce_hash: Mapped[str | None] = mapped_column(String(64))


class OAuthGrant(Base):
    """What the operator allowed one client: the groups and levels of an API token. Every token
    issued from one consent belongs to it, so revoking it ends that whole family."""

    __tablename__ = "oauth_grants"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("oauth_clients.id"), index=True)
    permissions: Mapped[dict[str, str]] = mapped_column(JSON)
    resource: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class OAuthCode(Base):
    """An authorization code: one use, one minute. A used one is kept until it expires, so a
    second use is recognised and ends the grant it produced."""

    __tablename__ = "oauth_codes"

    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("oauth_clients.id"), index=True)
    permissions: Mapped[dict[str, str]] = mapped_column(JSON)
    # PKCE challenge, redirect URI and resource, from the authorization request.
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    grant_id: Mapped[str | None] = mapped_column(ForeignKey("oauth_grants.id"))


class OAuthToken(Base):
    """An access or refresh token. A used refresh token stays until it expires, to recognise a
    replay; `parent_hash` names the refresh token a pair was issued from."""

    __tablename__ = "oauth_tokens"

    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[str] = mapped_column(ForeignKey("oauth_grants.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))
    parent_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
