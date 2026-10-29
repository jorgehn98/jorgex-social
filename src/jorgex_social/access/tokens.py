import hashlib
import secrets
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from jorgex_social.access.models import ApiToken
from jorgex_social.db import utcnow

# Distinct prefix so secret scanners can recognise these tokens (and not confuse them with others).
TOKEN_PREFIX = "jxs_"
PREFIX_LENGTH = len(TOKEN_PREFIX) + 8


class Group(StrEnum):
    POSTS = "posts"
    SCHEDULING = "scheduling"
    RULES = "rules"
    EVENTS = "events"
    STATS = "stats"
    ACCOUNT = "account"


class Level(StrEnum):
    DENY = "deny"
    READ = "read"
    WRITE = "write"


_RANK = {Level.DENY: 0, Level.READ: 1, Level.WRITE: 2}


class TokenError(ValueError):
    pass


def hash_token(secret: str) -> str:
    # The secret is 256 random bits, so a fast hash is enough; a password hash would add nothing.
    return hashlib.sha256(secret.encode()).hexdigest()


def normalize_permissions(grants: dict[str, str]) -> dict[str, str]:
    """Every group gets an explicit level; anything not granted is denied."""
    permissions = {group.value: Level.DENY.value for group in Group}
    for group, level in grants.items():
        try:
            permissions[Group(group).value] = Level(level).value
        except ValueError:
            raise TokenError(f"unknown group or level: {group}={level}") from None
    return permissions


def create_token(session: Session, name: str, grants: dict[str, str]) -> tuple[ApiToken, str]:
    """Returns the stored token and its secret, which is never stored and cannot be shown again."""
    name = name.strip()
    if not name:
        raise TokenError("token name is required")
    if session.scalar(select(ApiToken.id).where(ApiToken.name == name)):
        raise TokenError(f"a token named {name!r} already exists")
    secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token = ApiToken(
        name=name,
        prefix=secret[:PREFIX_LENGTH],
        hash=hash_token(secret),
        permissions=normalize_permissions(grants),
    )
    session.add(token)
    session.flush()
    return token, secret


def revoke_token(session: Session, name: str) -> ApiToken:
    token = session.scalar(select(ApiToken).where(ApiToken.name == name))
    if token is None:
        raise TokenError(f"no token named {name!r}")
    token.revoked_at = token.revoked_at or utcnow()
    return token


def authenticate(session: Session, secret: str) -> ApiToken | None:
    token = session.scalar(select(ApiToken).where(ApiToken.hash == hash_token(secret)))
    if token is None or token.revoked_at is not None:
        return None
    token.last_used_at = utcnow()
    return token


def allows(token: ApiToken, group: Group, level: Level) -> bool:
    granted = Level(token.permissions.get(group.value, Level.DENY.value))
    return _RANK[granted] >= _RANK[level] > _RANK[Level.DENY]
