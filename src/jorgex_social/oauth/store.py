"""The OAuth authorization server's state on SQLite: clients, pending requests, codes, grants and
tokens. Every function runs inside the caller's transaction and takes the time from the caller,
so the app's clock decides every expiry. Secrets are returned once and stored only as hashes."""

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from mcp.server.auth.provider import construct_redirect_uri
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from jorgex_social.access.tokens import hash_token
from jorgex_social.account import audit
from jorgex_social.account.models import ActorType, Outcome
from jorgex_social.oauth.models import OAuthClient, OAuthCode, OAuthGrant, OAuthRequest, OAuthToken

ACCESS_LIFETIME = timedelta(hours=1)
REFRESH_LIFETIME = timedelta(days=30)
CODE_LIFETIME = timedelta(seconds=60)
REQUEST_LIFETIME = timedelta(minutes=10)
# A registered client nobody approved is forgotten after this: registration is open to anyone.
UNCONSENTED_CLIENT_LIFETIME = timedelta(hours=1)
# A refresh token presented again this soon after its first use is a retry after a lost answer,
# not a replay: the pair issued then is replaced instead of ending the grant.
REFRESH_GRACE = timedelta(seconds=30)
# Distinct prefixes, so a leaked token says what it is and the API can tell them from its own.
ACCESS_PREFIX = "jxo_"
REFRESH_PREFIX = "jxr_"
ACCESS = "access"
REFRESH = "refresh"


class OAuthError(ValueError):
    pass


class Invalid(Exception):
    """A code or refresh token that cannot be used: unknown, expired, used or revoked."""


class Replayed(Invalid):
    """A used code, or a used refresh token past its grace, came back. The caller ends the grant
    (code_replayed, refresh_replayed) in a transaction of its own: this one rolls back."""


@dataclass(frozen=True)
class Issued:
    access: str
    refresh: str
    grant_id: str


@dataclass(frozen=True)
class Access:
    """What a valid access token allows. Duck-types the API token where the API authorizes
    (`permissions`) and audits (`prefix`)."""

    grant_id: str
    client_id: str
    client_name: str | None
    permissions: dict[str, str]
    expires_at: datetime
    # The resource (RFC 8707) its grant was issued for.
    resource: str

    @property
    def name(self) -> str:
        return f"oauth:{self.client_name or self.client_id}"

    @property
    def prefix(self) -> str:
        return f"oauth:{self.grant_id}"


def _audit(
    session: Session,
    now: datetime,
    action: str,
    grant: OAuthGrant | None = None,
    client_id: str | None = None,
    outcome: Outcome = Outcome.OK,
    *,
    by_operator: bool = False,
    detail: dict[str, Any] | None = None,
) -> None:
    audit.record(
        session,
        now,
        action,
        outcome,
        actor_type=ActorType.OPERATOR if by_operator else ActorType.SYSTEM,
        actor_ref=f"oauth:{grant.id}" if grant else None,
        object_type="oauth_grant" if grant else "oauth_client",
        object_id=grant.id if grant else client_id,
        detail=detail,
    )


def purge(session: Session, now: datetime) -> None:
    """Drops what has expired, and the clients registered over an hour ago and never approved."""
    session.execute(delete(OAuthRequest).where(OAuthRequest.expires_at <= now))
    session.execute(delete(OAuthCode).where(OAuthCode.expires_at <= now))
    session.execute(delete(OAuthToken).where(OAuthToken.expires_at <= now))
    stale = select(OAuthClient.id).where(
        OAuthClient.consented_at.is_(None),
        OAuthClient.created_at <= now - UNCONSENTED_CLIENT_LIFETIME,
    )
    session.execute(delete(OAuthRequest).where(OAuthRequest.client_id.in_(stale)))
    session.execute(delete(OAuthClient).where(OAuthClient.id.in_(stale)))


# Clients


def add_client(
    session: Session, client_id: str, name: str | None, info: dict[str, Any], now: datetime
) -> None:
    purge(session, now)
    session.add(OAuthClient(id=client_id, name=name, info=info, created_at=now))


def find_client(session: Session, client_id: str, now: datetime) -> OAuthClient | None:
    found = session.get(OAuthClient, client_id)
    if found is None:
        return None
    if found.consented_at is None and found.created_at <= now - UNCONSENTED_CLIENT_LIFETIME:
        return None
    return found


# Authorization requests and consent


def create_request(session: Session, client_id: str, params: dict[str, Any], now: datetime) -> str:
    purge(session, now)
    request_id = secrets.token_urlsafe(32)
    session.add(
        OAuthRequest(
            id_hash=hash_token(request_id),
            client_id=client_id,
            params=params,
            expires_at=now + REQUEST_LIFETIME,
        )
    )
    return request_id


def _pending(
    session: Session, request_id: str, session_hash: str, now: datetime
) -> OAuthRequest | None:
    found = session.get(OAuthRequest, hash_token(request_id))
    if found is None or found.expires_at <= now:
        return None
    if found.session_hash is not None and found.session_hash != session_hash:
        return None
    return found


def open_request(
    session: Session, request_id: str, session_hash: str, now: datetime
) -> tuple[OAuthRequest, OAuthClient, str] | None:
    """For the consent page: binds the request to the first browser session that opens it and
    issues the form's one-use value. None if it is unknown, expired or another session's."""
    pending = _pending(session, request_id, session_hash, now)
    client = pending and find_client(session, pending.client_id, now)
    if pending is None or client is None:
        return None
    nonce = secrets.token_urlsafe(32)
    pending.session_hash = session_hash
    pending.nonce_hash = hash_token(nonce)
    return pending, client, nonce


def decide(
    session: Session,
    request_id: str,
    nonce: str,
    session_hash: str,
    permissions: dict[str, str] | None,
    now: datetime,
) -> str | None:
    """The operator's answer: the URI to send the browser back to, with a code, or with
    `access_denied` when `permissions` is None. The request is used up either way. None when the
    request, its session or its one-use value do not match: then nothing is sent anywhere."""
    pending = _pending(session, request_id, session_hash, now)
    if (
        pending is None
        or pending.nonce_hash is None
        or pending.session_hash != session_hash
        or not hmac.compare_digest(pending.nonce_hash, hash_token(nonce))
    ):
        return None
    session.delete(pending)
    client = find_client(session, pending.client_id, now)
    if client is None:
        return None
    params = pending.params
    if permissions is None:
        _audit(session, now, "oauth.consent", None, client.id, Outcome.DENIED, by_operator=True)
        return construct_redirect_uri(
            params["redirect_uri"], error="access_denied", state=params["state"]
        )
    code = secrets.token_urlsafe(32)
    session.add(
        OAuthCode(
            hash=hash_token(code),
            client_id=client.id,
            permissions=permissions,
            params={
                key: params[key]
                for key in (
                    "code_challenge",
                    "redirect_uri",
                    "redirect_uri_provided_explicitly",
                    "resource",
                )
            },
            expires_at=now + CODE_LIFETIME,
        )
    )
    client.consented_at = client.consented_at or now
    _audit(
        session,
        now,
        "oauth.consent",
        None,
        client.id,
        by_operator=True,
        detail={"permissions": permissions},
    )
    return construct_redirect_uri(params["redirect_uri"], code=code, state=params["state"])


# Codes


def find_code(session: Session, code: str, client_id: str, now: datetime) -> OAuthCode | None:
    """Only reads. A used code is returned (check `used_at`), so its replay can be dealt with."""
    found = session.get(OAuthCode, hash_token(code))
    if found is None or found.client_id != client_id or found.expires_at <= now:
        return None
    return found


def _issue(session: Session, grant: OAuthGrant, parent_hash: str | None, now: datetime) -> Issued:
    access = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    for secret, kind, lifetime in (
        (access, ACCESS, ACCESS_LIFETIME),
        (refresh, REFRESH, REFRESH_LIFETIME),
    ):
        session.add(
            OAuthToken(
                hash=hash_token(secret),
                grant_id=grant.id,
                kind=kind,
                parent_hash=parent_hash,
                created_at=now,
                expires_at=now + lifetime,
            )
        )
    return Issued(access, refresh, grant.id)


def _end(session: Session, grant: OAuthGrant, now: datetime, reason: str, **who) -> None:
    grant.revoked_at = now
    session.execute(delete(OAuthToken).where(OAuthToken.grant_id == grant.id))
    _audit(session, now, "oauth.revoked", grant, detail={"reason": reason}, **who)


def code_replayed(session: Session, code: str, now: datetime) -> None:
    """A used code came back: whoever holds it may have the tokens too, so the grant ends."""
    found = session.get(OAuthCode, hash_token(code))
    grant = found and found.grant_id and session.get(OAuthGrant, found.grant_id)
    if grant and grant.revoked_at is None:
        _end(session, grant, now, "code_reused")


def exchange_code(session: Session, code: str, client_id: str, now: datetime) -> Issued:
    purge(session, now)
    found = find_code(session, code, client_id, now)
    if found is None:
        raise Invalid
    if found.used_at is not None:
        raise Replayed
    found.used_at = now
    grant = OAuthGrant(
        id=secrets.token_hex(8),
        client_id=client_id,
        permissions=found.permissions,
        resource=found.params["resource"],
        created_at=now,
    )
    session.add(grant)
    session.flush()
    found.grant_id = grant.id
    issued = _issue(session, grant, None, now)
    _audit(session, now, "oauth.token_issued", grant)
    return issued


# Refresh tokens


def _live_refresh(
    session: Session, secret: str, client_id: str, now: datetime
) -> tuple[OAuthToken, OAuthGrant] | None:
    found = session.get(OAuthToken, hash_token(secret))
    if found is None or found.kind != REFRESH or found.expires_at <= now:
        return None
    grant = session.get(OAuthGrant, found.grant_id)
    if grant is None or grant.revoked_at is not None or grant.client_id != client_id:
        return None
    return found, grant


def _retry(session: Session, token: OAuthToken, now: datetime) -> bool:
    """A used refresh token presented again within the grace period, while the pair it gave has
    not been used either: the client most likely never received that answer. Once a retry is
    answered, that pair counts as used (rotate), so only one retry is tolerated. A token marked
    used by such a retry never produced a pair: it is never a retry itself, or the two holders
    could each keep rotating their own branch of the family."""
    if token.used_at is None or now - token.used_at > REFRESH_GRACE:
        return False
    children = session.scalars(
        select(OAuthToken.used_at).where(
            OAuthToken.parent_hash == token.hash, OAuthToken.kind == REFRESH
        )
    ).all()
    return bool(children) and all(used_at is None for used_at in children)


def check_refresh(session: Session, secret: str, client_id: str, now: datetime) -> bool | None:
    """Only reads. True if it can be exchanged, False if it is a replay, None if unknown."""
    found = _live_refresh(session, secret, client_id, now)
    if found is None:
        return None
    token, _ = found
    return token.used_at is None or _retry(session, token, now)


def refresh_replayed(session: Session, secret: str, client_id: str, now: datetime) -> None:
    """A refresh token already exchanged came back after the grace period: one of the two
    holders is not the client, and there is no telling which, so the whole family ends."""
    found = _live_refresh(session, secret, client_id, now)
    if found is None:
        return
    token, grant = found
    if token.used_at is not None and not _retry(session, token, now):
        _audit(session, now, "oauth.refresh_reused", grant, outcome=Outcome.DENIED)
        _end(session, grant, now, "refresh_reused")


def rotate(session: Session, secret: str, client_id: str, now: datetime) -> Issued:
    """Exchanges a refresh token for a new pair; the old access token stops working."""
    purge(session, now)
    found = _live_refresh(session, secret, client_id, now)
    if found is None:
        raise Invalid
    token, grant = found
    if token.used_at is not None:
        if not _retry(session, token, now):
            raise Replayed
        # The pair issued on the first use most likely never reached the client. Its refresh
        # token is not deleted but marked used: if it did reach someone, presenting it later
        # than the grace period is a replay that ends the grant, instead of a silent takeover by
        # whoever retried. (Its access token goes with every other below.)
        session.execute(
            update(OAuthToken)
            .where(OAuthToken.parent_hash == token.hash, OAuthToken.used_at.is_(None))
            .values(used_at=now)
        )
    else:
        token.used_at = now
    session.execute(
        delete(OAuthToken).where(OAuthToken.grant_id == grant.id, OAuthToken.kind == ACCESS)
    )
    issued = _issue(session, grant, token.hash, now)
    _audit(session, now, "oauth.token_refreshed", grant)
    return issued


# Access tokens


def find_access(
    session: Session, secret: str, resource: str | None, now: datetime
) -> Access | None:
    """Only reads. None unless the token is live, its grant stands and, if `resource` is given,
    it was issued for it."""
    found = session.get(OAuthToken, hash_token(secret))
    if found is None or found.kind != ACCESS or found.expires_at <= now:
        return None
    grant = session.get(OAuthGrant, found.grant_id)
    if grant is None or grant.revoked_at is not None:
        return None
    if resource is not None and grant.resource != resource:
        return None
    client = session.get(OAuthClient, grant.client_id)
    return Access(
        grant_id=grant.id,
        client_id=grant.client_id,
        client_name=client.name if client else None,
        permissions=grant.permissions,
        expires_at=found.expires_at,
        resource=grant.resource,
    )


# Revocation


def revocable(session: Session, secret: str, client_id: str) -> OAuthGrant | None:
    """Only reads. The grant a client's revocation request (RFC 7009) would end. A refresh token
    already exchanged ends nothing: clients revoke the one they hold, and an old one in a
    revocation request is not a replay either."""
    found = session.get(OAuthToken, hash_token(secret))
    if found is None or (found.kind == REFRESH and found.used_at is not None):
        return None
    grant = session.get(OAuthGrant, found.grant_id)
    if grant is None or grant.revoked_at is not None or grant.client_id != client_id:
        return None
    return grant


def revoke_by_client(session: Session, secret: str, client_id: str, now: datetime) -> None:
    grant = revocable(session, secret, client_id)
    if grant is not None:
        _end(session, grant, now, "client")


def revoke_grant(session: Session, grant_id: str, now: datetime) -> OAuthGrant:
    """For the operator. The client's registration stays: a client that still holds its id
    would otherwise be stuck until it is logged out by hand."""
    grant = session.get(OAuthGrant, grant_id)
    if grant is None:
        raise OAuthError(f"no grant with id {grant_id!r}")
    if grant.revoked_at is None:
        _end(session, grant, now, "operator", by_operator=True)
    return grant


def revoke_all(session: Session, now: datetime, reason: str) -> int:
    grants = session.scalars(select(OAuthGrant).where(OAuthGrant.revoked_at.is_(None))).all()
    for grant in grants:
        _end(session, grant, now, reason, by_operator=True)
    return len(grants)


def list_grants(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(OAuthGrant, OAuthClient.name)
        .join(OAuthClient, OAuthClient.id == OAuthGrant.client_id)
        .order_by(OAuthGrant.created_at)
    )
    return [
        {
            "id": grant.id,
            "client_id": grant.client_id,
            "client_name": name,
            "permissions": grant.permissions,
            "created_at": grant.created_at.isoformat(),
            "revoked_at": grant.revoked_at and grant.revoked_at.isoformat(),
        }
        for grant, name in rows
    ]
