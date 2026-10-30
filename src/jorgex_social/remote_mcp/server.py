"""The remote MCP at /mcp (Streamable HTTP), for agents that connect with only the URL and OAuth.

It registers the local MCP's tool definitions (jorgex_social_mcp) and runs each call against this
same app, in process, with the OAuth access token of the request: the API authorizes it by the
grant's groups and levels, exactly as for any other client, and a refusal reaches the agent as a
tool error. Every tool is registered, since permissions belong to the token, not the process.

The definitions are synchronous (urllib on the agent's machine). Here each runs in a worker
thread, and its HTTP call hops back to the event loop to reach the app through httpx2's ASGI
transport, so a tool never blocks the loop and never calls the service over the network."""

import contextlib
import functools
import inspect
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import anyio
import anyio.from_thread
import anyio.to_thread
import httpx2
from fastapi import FastAPI
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from jorgex_social.remote_mcp import uploads
from jorgex_social_mcp.client import Api, ApiError, Body
from jorgex_social_mcp.server import INSTRUCTIONS, WRITE, tool_definitions

# Requests made in process carry no forwarding header, so the app does not take them for the
# proxy's (access/proxy.py), and only the token of the MCP request.
IN_PROCESS = "http://in-process"
# Tool calls running at once; each holds a thread while its request runs on the loop.
TOOL_THREADS = 8
# Reads a local path, which on the server would be the server's: replaced by create_upload_url.
LOCAL_ONLY = {"upload_media"}
LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def remote_definitions(api: Api) -> list[tuple[str, str, Callable[..., str], Any]]:
    def create_upload_url() -> str:
        """Get a one-use URL, valid ten minutes, to upload one .jpg, .mp4 or .mov file from your
        machine (up to max_bytes). Send the file as the raw body of a POST, for example
        `curl --data-binary @/absolute/path/photo.jpg '<url>'`; the answer is the stored media,
        whose id goes in create_post or update_post. The URL is the only credential: send no
        token with it, and do not share it."""
        try:
            return json.dumps(api.request("POST", "/v1/upload-urls"))
        except ApiError as exc:
            raise ToolError(exc.args[0]) from None

    shared = [d for d in tool_definitions(api) if d[2].__name__ not in LOCAL_ONLY]
    return [*shared, ("posts", "write", create_upload_url, WRITE)]


class InProcessTools:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.threads = anyio.CapacityLimiter(TOOL_THREADS)

    def transport(self, method: str, url: str, headers: dict[str, str], body: Body):
        """The local client's transport, called from a tool's worker thread."""
        return anyio.from_thread.run(self._send, method, url, headers, body)

    async def _send(self, method: str, url: str, headers: dict[str, str], body: Body):
        asgi = httpx2.ASGITransport(app=self.app)
        async with httpx2.AsyncClient(transport=asgi) as client:
            response = await client.request(method, url, headers=headers, content=body)
        return response.status_code, response.content

    def tool(self, template: Callable[..., str]) -> Callable[..., Any]:
        """An async tool with the template's name, description and parameters, plus the
        request's Context, through which it gets the request's access token."""
        name = template.__name__

        async def tool(ctx: Context, **arguments: Any) -> str:
            token = ctx.request_context.request.scope["user"].access_token.token
            api = Api(IN_PROCESS, token, self.transport)
            run = next(d[2] for d in remote_definitions(api) if d[2].__name__ == name)
            call = functools.partial(run, **arguments)
            return await anyio.to_thread.run_sync(call, limiter=self.threads)

        signature = inspect.signature(template, eval_str=True)
        context = inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)
        tool.__name__ = tool.__qualname__ = name
        tool.__doc__ = template.__doc__
        tool.__signature__ = signature.replace(parameters=[*signature.parameters.values(), context])
        tool.__annotations__ = {
            **{p.name: p.annotation for p in signature.parameters.values()},
            "ctx": Context,
            "return": str,
        }
        return tool


def _transport_security(public_url: str) -> TransportSecuritySettings:
    """The public host for the proxy, and loopback for the SSH tunnel."""
    parts = urlsplit(public_url)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[parts.netloc, *LOOPBACK_HOSTS],
        allowed_origins=[f"{parts.scheme}://{parts.netloc}", *LOOPBACK_ORIGINS],
    )


def install(app: FastAPI) -> None:
    """With OAuth only (oauth/routes.py): /mcp, the upload URLs and the lifespan that runs the
    MCP session manager (without it, every /mcp request fails)."""
    provider = getattr(app.state, "oauth_provider", None)
    if app.state.oauth_resource is None or provider is None:
        return
    server = MCPServer(
        "jorgex-social",
        instructions=INSTRUCTIONS
        + " Upload files with create_upload_url: it gives a one-use URL to send the file to.",
        token_verifier=ProviderTokenVerifier(provider),
        auth=AuthSettings(
            issuer_url=provider.issuer,
            resource_server_url=provider.resource,
            # Refuses a token whose grant names another resource, as the API does.
            validate_token_resource=True,
        ),
    )
    tools = InProcessTools(app)
    for _, _, template, annotations in remote_definitions(Api(IN_PROCESS, "unused")):
        server.add_tool(tools.tool(template), annotations=annotations, structured_output=False)
    mcp_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=_transport_security(app.state.settings.public_url),
    )
    # Only /mcp reaches the MCP app (its bearer middleware and DNS-rebinding checks included);
    # every other path keeps the service's own routing and 404s.
    app.router.routes.append(Route("/mcp", endpoint=mcp_app))
    app.include_router(uploads.router)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        # Starlette does not run a mounted app's lifespan: the parent must start the manager.
        async with server.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
