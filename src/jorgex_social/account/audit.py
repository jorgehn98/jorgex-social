"""Append-only audit log: every API request that writes, and the transitions the worker makes
on its own. Entries carry identifiers only: never a request body, a secret or third-party text."""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jorgex_social.account.models import ActorType, AuditEntry, Outcome

log = logging.getLogger(__name__)

READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def record(
    session: Session,
    now: datetime,
    action: str,
    outcome: Outcome = Outcome.OK,
    *,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_ref: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEntry(
            occurred_at=now,
            actor_type=actor_type,
            actor_ref=actor_ref,
            action=action,
            object_type=object_type,
            object_id=object_id,
            outcome=outcome,
            detail=detail,
        )
    )


def outcome_of(status_code: int) -> Outcome:
    if status_code in {401, 403}:
        return Outcome.DENIED
    return Outcome.OK if status_code < 400 else Outcome.ERROR


def _object(path_params: dict[str, Any]) -> tuple[str | None, str | None]:
    # Routes name their identifiers `<type>_id` (post_id, incident_id), so the route says what
    # the request acts on without reading the body.
    for name, value in path_params.items():
        if name.endswith("_id"):
            return name.removesuffix("_id"), str(value)
    return None, None


# request.state lives in scope["state"], which the middleware reads after the response.
def set_actor(request: Request, token_prefix: str) -> None:
    """Called by authentication, so the entry names the token even when it was then denied."""
    request.state.audit_actor = token_prefix


def set_detail(request: Request, detail: dict[str, Any]) -> None:
    """For an endpoint whose route alone does not say what changed (the kill switch: on or off)."""
    request.state.audit_detail = detail


class AuditMiddleware:
    """Records every request that writes, after the response, in its own transaction. Plain ASGI
    rather than BaseHTTPMiddleware so large uploads stream through untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in READ_METHODS:
            await self.app(scope, receive, send)
            return
        status_code = 500

        async def send_and_capture(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_capture)
        finally:
            await run_in_threadpool(self._write, scope, status_code)

    def _write(self, scope: Scope, status_code: int) -> None:
        route = scope.get("route")
        if route is None:
            return  # no such endpoint: nothing was acted on, and the raw path may hold anything
        state = scope.get("state", {})
        object_type, object_id = _object(scope.get("path_params", {}))
        app = scope["app"]
        try:
            with app.state.sessionmaker() as session, session.begin():
                record(
                    session,
                    app.state.clock.now(),
                    f"{scope['method']} {route.path}",
                    outcome_of(status_code),
                    actor_type=ActorType.TOKEN,
                    actor_ref=state.get("audit_actor"),
                    object_type=object_type,
                    object_id=object_id,
                    detail=state.get("audit_detail"),
                )
        except Exception:
            # The response is already sent; losing one entry must not take the request down.
            log.exception("could not write the audit entry")
