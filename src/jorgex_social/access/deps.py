from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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


def _authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None) -> ApiToken:
    """Looks the token up in a read (deferred) transaction, so unknown tokens never take the write
    lock: a flood of made-up tokens cannot stall the worker. Only a valid token writes, briefly,
    and at most once per LAST_USED_RESOLUTION. Never holds a transaction across the request."""
    token = None
    if credentials:
        with request.app.state.read_sessionmaker() as session, session.begin():
            token = authenticate(session, credentials.credentials)
    if token is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Missing, unknown or revoked token."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    set_actor(request, token.prefix)  # before authorizing, so a denied call names its token
    now = request.app.state.clock.now()
    if needs_use_recorded(token, now):
        with request.app.state.sessionmaker() as session, session.begin():
            record_use(session, token, now)
    return token


def _authorize(token: ApiToken, group: Group, level: Level) -> ApiToken:
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


def current_token(request: Request, credentials: Credentials) -> ApiToken:
    return _authenticate(request, credentials)


TokenDep = Annotated[ApiToken, Depends(current_token)]


def require(group: Group, level: Level) -> Callable[[ApiToken], ApiToken]:
    """Authorizes each call on the server, per tool group, as decision 04 requires."""

    def dependency(token: TokenDep) -> ApiToken:
        return _authorize(token, group, level)

    return dependency


def require_outside_transaction(group: Group, level: Level) -> Callable[[ApiToken], ApiToken]:
    """For long requests such as uploads, which must not hold the database write lock while the
    body streams in. Authentication already runs in its own short transactions, so this is
    `require`; the name keeps that guarantee explicit where it matters."""
    return require(group, level)
