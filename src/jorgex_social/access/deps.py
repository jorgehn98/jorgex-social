from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from jorgex_social.access.models import ApiToken
from jorgex_social.access.tokens import Group, Level, allows, authenticate

_bearer = HTTPBearer(auto_error=False)


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session, session.begin():
        yield session


# scope="function": commit before the response is sent, so a failed commit is a 500, not a 2xx.
SessionDep = Annotated[Session, Depends(get_session, scope="function")]


def current_token(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ApiToken:
    token = authenticate(session, credentials.credentials) if credentials else None
    if token is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Missing, unknown or revoked token."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


TokenDep = Annotated[ApiToken, Depends(current_token)]


def require(group: Group, level: Level) -> Callable[[ApiToken], ApiToken]:
    """Authorizes each call on the server, per tool group, as decision 04 requires."""

    def dependency(token: TokenDep) -> ApiToken:
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

    return dependency
