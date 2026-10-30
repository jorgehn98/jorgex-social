import logging
from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from jorgex_social.access.models import ApiToken
from jorgex_social.access.tokens import (
    Group,
    Level,
    allows,
    authenticate,
    needs_use_recorded,
    record_use,
)
from jorgex_social.account.audit import set_actor
from jorgex_social.oauth import store as oauth

log = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session, session.begin():
        yield session


# scope="function": commit before the response is sent, so a failed commit is a 500, not a 2xx.
SessionDep = Annotated[Session, Depends(get_session, scope="function")]


def get_read_session(request: Request) -> Iterator[Session]:
    with request.app.state.read_sessionmaker() as session, session.begin():
        yield session


# Read-only endpoints that must not wait for the write lock; never write through it.
ReadSessionDep = Annotated[Session, Depends(get_read_session, scope="function")]


# An API token, or an OAuth access token with the groups and levels the operator granted.
Principal = ApiToken | oauth.Access


def _find(request: Request, session, secret: str) -> Principal | None:
    if not secret.startswith(oauth.ACCESS_PREFIX):
        return authenticate(session, secret)
    resource = request.app.state.oauth_resource  # None without a public URL: no OAuth at all
    return resource and oauth.find_access(session, secret, resource, request.app.state.clock.now())


def _authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None) -> Principal:
    """Looks the token up in a read (deferred) transaction, so unknown tokens never take the write
    lock: a flood of made-up tokens cannot stall the worker. Only a valid API token writes,
    briefly, and at most once per LAST_USED_RESOLUTION. Never holds a transaction across the
    request."""
    token = None
    if credentials:
        with request.app.state.read_sessionmaker() as session, session.begin():
            token = _find(request, session, credentials.credentials)
    if token is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Missing, unknown or revoked token."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    set_actor(request, token.prefix)  # before authorizing, so a denied call names its token
    now = request.app.state.clock.now()
    if isinstance(token, ApiToken) and needs_use_recorded(token, now):
        try:
            with request.app.state.sessionmaker() as session, session.begin():
                record_use(session, token, now)
        except OperationalError:
            # Only bookkeeping (a lock timeout): the request goes on; the next use retries.
            log.warning("could not record the last use of token %s", token.prefix, exc_info=True)
    return token


def _authorize(token: Principal, group: Group, level: Level) -> Principal:
    if not allows(token, group, level):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "code": "forbidden",
                "message": f"This token cannot {level.value} the {group.value} group.",
                "group": group.value,
                "required": level.value,
            },
        )
    return token


Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def current_token(request: Request, credentials: Credentials) -> Principal:
    return _authenticate(request, credentials)


TokenDep = Annotated[Principal, Depends(current_token)]


def require(group: Group, level: Level) -> Callable[[Principal], Principal]:
    """Authorizes each call on the server, per tool group, as decision 04 requires."""

    def dependency(token: TokenDep) -> Principal:
        return _authorize(token, group, level)

    return dependency


def require_outside_transaction(group: Group, level: Level) -> Callable[[Principal], Principal]:
    """For long requests such as uploads, which must not hold the database write lock while the
    body streams in. Authentication already runs in its own short transactions, so this is
    `require`; the name keeps that guarantee explicit where it matters."""
    return require(group, level)
