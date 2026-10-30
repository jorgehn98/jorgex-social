"""The authorization server's routes: the SDK's metadata, authorization, token and registration
endpoints, our own revocation, and the consent page inside the interface, where the operator's
session and its cookie (Path=/ui) already live."""

import logging
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import (
    build_metadata,
    cors_middleware,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware
from pydantic import AnyHttpUrl
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route, request_response
from starlette.types import ASGIApp, Receive, Scope, Send

from jorgex_social.access.limits import RateLimiter, client_key
from jorgex_social.access.tokens import Group, Level, TokenError, normalize_permissions
from jorgex_social.oauth import store
from jorgex_social.oauth.provider import Provider
from jorgex_social.ui.routes import HEADERS, RecentSignIn, read_form, render, same_origin

log = logging.getLogger(__name__)

# Nothing legitimate sent to these endpoints comes near this.
MAX_BODY = 16 * 1024
# Per client address (IPv6: per /64). Registration is the one anyone can use to write rows.
REGISTER_LIMITS = [(10, timedelta(minutes=1)), (30, timedelta(hours=1))]
ENDPOINT_LIMITS = [(30, timedelta(minutes=1)), (300, timedelta(hours=1))]
GONE = (
    "This authorization request is unknown, expired, already answered or was opened in another "
    "browser. Start the connection again from the client."
)
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class Guarded:
    """Counts the request against the client's address, then caps its body. A class, not a
    function: Starlette would take a function for a request handler."""

    def __init__(self, app: ASGIApp, limiter: str) -> None:
        self.app = RequestBodyLimitMiddleware(app, MAX_BODY)
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        state = scope["app"].state
        client = client_key(scope["client"][0] if scope.get("client") else None)
        wait = getattr(state, self.limiter).hit(client, state.clock.now())
        if wait is None:
            await self.app(scope, receive, send)
            return
        log.warning("oauth request refused: too many", extra={"client": client})
        response = JSONResponse(
            {"error": "temporarily_unavailable", "error_description": "too many requests"},
            status_code=429,
            headers={**NO_STORE, "Retry-After": str(int(wait.total_seconds()) + 1)},
        )
        await response(scope, receive, send)


async def revoke(request: Request) -> Response:
    form = await request.form()
    client_id, token = form.get("client_id"), form.get("token")
    if not isinstance(client_id, str) or not isinstance(token, str):
        return JSONResponse({"error": "invalid_request"}, status_code=400, headers=NO_STORE)
    provider: Provider = request.app.state.oauth_provider
    if await provider.get_client(client_id) is None:
        return JSONResponse({"error": "invalid_client"}, status_code=401, headers=NO_STORE)
    await provider.revoke(token, client_id)
    # RFC 7009: the same answer whether or not the token was known.
    return Response(status_code=200, headers=NO_STORE)


def _routes(provider: Provider) -> list[Route]:
    issuer = AnyHttpUrl(provider.issuer)
    registration = ClientRegistrationOptions(enabled=True)
    # The SDK's revocation endpoint is left off: it requires a client secret, which public
    # clients do not have. Ours is announced in the metadata instead.
    sdk = create_auth_routes(provider, issuer, client_registration_options=registration)
    metadata = build_metadata(issuer, None, registration, RevocationOptions())
    metadata.token_endpoint_auth_methods_supported = ["none"]
    metadata.revocation_endpoint = AnyHttpUrl(f"{provider.issuer}/revoke")
    metadata.revocation_endpoint_auth_methods_supported = ["none"]

    routes = [
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        ),
        *create_protected_resource_routes(
            AnyHttpUrl(provider.resource), [issuer], resource_name="jorgex-social"
        ),
        Route(
            "/revoke",
            endpoint=Guarded(request_response(revoke), "oauth_limiter"),
            methods=["POST"],
        ),
    ]
    for route in sdk:
        if route.path == "/.well-known/oauth-authorization-server":
            continue
        limiter = "oauth_register_limiter" if route.path == "/register" else "oauth_limiter"
        methods = sorted((route.methods or set()) - {"HEAD"})
        routes.append(Route(route.path, endpoint=Guarded(route.app, limiter), methods=methods))
    return routes


# Consent

consent = APIRouter(
    prefix="/ui/oauth", include_in_schema=False, dependencies=[Depends(same_origin)]
)


def _origin(uri: str) -> str:
    parts = urlsplit(uri)
    return f"{parts.scheme}://{parts.netloc}"


@consent.get("/consent")
def consent_page(request: Request, signed_in: RecentSignIn, req: str = ""):
    state = request.app.state
    with state.sessionmaker() as session, session.begin():
        opened = store.open_request(session, req, signed_in.id_hash, state.clock.now())
        if opened is not None:
            pending, client, nonce = opened
            client_name, redirect_uri = client.name, pending.params["redirect_uri"]
    if opened is None:
        return render(request, "consent.html", 400, error=GONE)
    target = urlsplit(redirect_uri)
    response = render(
        request,
        "consent.html",
        client_name=client_name,
        host=target.hostname,
        port=target.port,
        groups=[group.value for group in Group],
        levels=[Level.WRITE.value, Level.READ.value, Level.DENY.value],
        req=req,
        nonce=nonce,
    )
    # A browser checks the redirect after the form is sent against this page's form-action, so
    # this page alone allows the exact origin its answer goes back to (spike, Chromium).
    response.headers["Content-Security-Policy"] = HEADERS["Content-Security-Policy"].replace(
        "form-action 'self'", f"form-action 'self' {_origin(redirect_uri)}"
    )
    return response


@consent.post("/consent")
async def consent_answer(request: Request, signed_in: RecentSignIn):
    form = await read_form(request)
    decision = form.get("decision")
    permissions = None
    if decision == "allow":
        try:
            permissions = normalize_permissions(
                {group.value: form.get(group.value, Level.DENY.value) for group in Group}
            )
        except TokenError:
            return render(request, "consent.html", 400, error=GONE)
    elif decision != "deny":
        return render(request, "consent.html", 400, error=GONE)
    state = request.app.state

    def answer() -> str | None:
        with state.sessionmaker() as session, session.begin():
            return store.decide(
                session,
                form.get("req", ""),
                form.get("nonce", ""),
                signed_in.id_hash,
                permissions,
                state.clock.now(),
            )

    location = await run_in_threadpool(answer)
    if location is None:
        # Never a redirect: nothing about this request can be trusted to say where to.
        return render(request, "consent.html", 400, error=GONE)
    # 303, so the browser follows with a GET instead of sending the form again.
    return RedirectResponse(location, status_code=303, headers=HEADERS)


def install(app: FastAPI) -> None:
    """Only with a public URL: it is the issuer and names the resource tokens are bound to."""
    settings = app.state.settings
    app.state.oauth_resource = None
    if not settings.public_url:
        return
    provider = Provider(
        app.state, settings.public_url, f"{settings.public_url}/mcp", settings.oauth_redirect_uris
    )
    app.state.oauth_provider = provider
    app.state.oauth_resource = provider.resource
    app.state.oauth_limiter = RateLimiter(ENDPOINT_LIMITS)
    app.state.oauth_register_limiter = RateLimiter(REGISTER_LIMITS)
    app.router.routes.extend(_routes(provider))
    app.include_router(consent)
