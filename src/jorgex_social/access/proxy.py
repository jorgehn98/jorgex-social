"""What the reverse proxy may reach. The proxy's own allowlist decides what is public; this is the
second check, so a mistake there does not publish the API.

The signal is the forwarding header itself, not the peer address: inside Docker the proxy and the
SSH tunnel both reach the service through the same published port, so they arrive from the same
address (the network's gateway). The proxy always sets X-Forwarded-For, and a client on the
internet cannot make it drop the header; someone on the tunnel can add one, which only hides the
API from themselves. uvicorn's ProxyHeadersMiddleware leaves the header in place, whether or not it
trusts the peer, so it is seen here either way."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Headers a reverse proxy sets on the requests it forwards.
FORWARDING_HEADERS = {b"x-forwarded-for", b"forwarded", b"x-real-ip"}
# Reached only through the SSH tunnel: the API and its description.
TUNNEL_ONLY = ("/v1", "/docs", "/redoc", "/openapi.json")


def via_proxy(scope: Scope) -> bool:
    return any(name in FORWARDING_HEADERS for name, _ in scope["headers"])


def tunnel_only(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in TUNNEL_ONLY)


class TunnelOnlyMiddleware:
    """Answers 404, as for any unknown path, to the API and its description when the request came
    through the proxy."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and tunnel_only(scope["path"]) and via_proxy(scope):
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)
