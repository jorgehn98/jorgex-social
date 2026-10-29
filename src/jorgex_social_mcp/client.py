"""Minimal client for the jorgex-social REST API, on the standard library.

It runs on the agent's machine and imports nothing from the service, so it only depends on the
HTTP contract.
"""

import ipaddress
import json
from collections.abc import Callable
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

TIMEOUT_SECONDS = 120

Body = bytes | BinaryIO | None
# (method, url, headers, body) -> (status, body). Swapped in tests; the default is urllib.
Transport = Callable[[str, str, dict[str, str], Body], tuple[int, bytes]]


class _NoRedirect(HTTPRedirectHandler):
    # urllib would forward the Authorization header to wherever a redirect points.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = build_opener(_NoRedirect)


def urllib_transport(method: str, url: str, headers: dict[str, str], body: Body):
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with _opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


class ApiError(Exception):
    """An error answer from the API, or no answer at all. `payload` is what the agent sees."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(json.dumps(payload))
        self.payload = payload


def check_url(url: str) -> None:
    """The token goes in every request, so it only travels in the clear to this same machine."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "https" and host:
        return
    if parts.scheme == "http" and (host == "localhost" or _is_loopback(host)):
        return
    raise ValueError("JORGEX_SOCIAL_URL must use https, or http only to localhost.")


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def segment(value: str) -> str:
    """A path parameter, escaped so an id cannot point at another endpoint."""
    return quote(value, safe="")


class Api:
    def __init__(self, base_url: str, token: str, transport: Transport = urllib_transport):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        raw: tuple[BinaryIO, str, int] | None = None,
    ) -> Any:
        """`raw` is (file, content type, size): the body is streamed as is."""
        url = self._base_url + path
        params = {k: _query_value(v) for k, v in (query or {}).items() if v is not None}
        if params:
            url += "?" + urlencode(params)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        body: Body = None
        if raw is not None:
            body, headers["Content-Type"], size = raw
            headers["Content-Length"] = str(size)
        elif json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        try:
            status, content = self._transport(method, url, headers, body)
        except (URLError, OSError) as exc:
            # The reason names the host or the socket error, never the request headers.
            reason = getattr(exc, "reason", exc)
            raise ApiError(
                {
                    "status": None,
                    "code": "unreachable",
                    "message": f"Cannot reach the API: {reason}",
                }
            ) from None
        try:
            data = json.loads(content) if content else None
        except ValueError:
            data = content[:500].decode(errors="replace")
        if status >= 400:
            detail = data.get("detail", data) if isinstance(data, dict) else data
            # The API's own errors carry {code, message, ...}; anything else (FastAPI validation
            # errors, a proxy page) is passed through under `detail`.
            if isinstance(detail, dict) and "code" in detail:
                raise ApiError({"status": status, **detail})
            raise ApiError({"status": status, "code": "http_error", "detail": detail})
        return data


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
