# Fixtures come from test_oauth by import, so each test parameter "redefines" one.
# ruff: noqa: F401, F811

import contextlib
import json
import logging
import re
import socket
import threading
import time
import urllib.request
from dataclasses import replace

import anyio
import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from test_oauth import (  # the OAuth fixtures and helpers
    PUBLIC,
    RESOURCE,
    agent,
    api,
    app,
    browser,
    clock,
    connect,
    operator_oauth,
    refresh,
    revoke,
    rows,
    settings,
)

from jorgex_social.access.tokens import create_token
from jorgex_social.api import create_app
from jorgex_social.logs import hide_query_strings
from jorgex_social.publishing import catalog

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64


@contextlib.asynccontextmanager
async def mcp_client(app, auth=None, headers=None):
    """The SDK's MCP client over Streamable HTTP, against the app with its lifespan running."""
    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=PUBLIC,
            auth=auth,
            headers=headers or {},
        )
        async with http, Client(streamable_http_client(RESOURCE, http_client=http)) as client:
            yield client


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def call(client, name, /, **arguments):
    result = await client.call_tool(name, arguments)
    text = result.content[0].text if result.content else "null"
    if result.is_error:
        prefix = f"Error executing tool {name}: "
        assert text.startswith(prefix), text
        return "error", json.loads(text.removeprefix(prefix))
    return "ok", json.loads(text)


# The seam: an MCP client that knows only the URL


@pytest.mark.anyio
async def test_an_agent_with_only_the_url_uses_the_tools_its_grant_allows(app, clock):
    transport = httpx2.ASGITransport(app=app)
    oauth, storage, _ = await operator_oauth(transport, clock, posts="write")

    async with mcp_client(app, auth=oauth) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        catalog_answer = await call(client, "get_catalog")
        denied = await call(client, "get_account")
        _, upload = await call(client, "create_upload_url")
        async with httpx2.AsyncClient(transport=transport) as curl:
            sent = await curl.post(upload["url"], content=JPEG)
        created = await call(client, "create_post", type="image", items=[sent.json()["id"]])

    # Every tool is registered (permissions are the token's), except the one that reads a local
    # path, which the upload URL replaces.
    assert {"get_account", "set_kill_switch", "create_upload_url", "create_post"} <= names
    assert "upload_media" not in names
    assert catalog_answer[0] == "ok" and "post_types" in catalog_answer[1]
    assert denied == ("error", denied[1])
    assert (denied[1]["status"], denied[1]["code"]) == (403, "forbidden")
    assert sent.status_code == 201 and sent.json()["type"] == "jpeg"
    assert created[0] == "ok" and created[1]["status"] == "draft"
    assert storage.tokens.access_token not in json.dumps(upload)


@pytest.mark.anyio
async def test_the_instructions_name_only_tools_the_remote_server_has(app, clock):
    oauth, _, _ = await operator_oauth(httpx2.ASGITransport(app=app), clock, posts="read")

    async with mcp_client(app, auth=oauth) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        referenced = set(re.findall(r"`([a-z_]+)`", client.instructions or ""))

    # A backticked identifier is always a tool name; uploads go through the upload URL here.
    assert referenced <= names, referenced - names
    assert {"create_upload_url", "create_post", "publish_now", "resolve_post"} <= referenced


@pytest.mark.anyio
async def test_without_a_token_mcp_points_to_its_metadata(app):
    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC)
        async with http:
            response = await http.post("/mcp", json={})

    assert response.status_code == 401
    expected = f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"'
    assert expected in response.headers["www-authenticate"]


@pytest.mark.anyio
async def test_a_revoked_or_unknown_token_is_refused(app, agent, browser):
    client_id, tokens = connect(agent, browser, posts="read")
    revoke(agent, client_id, tokens["access_token"])

    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC)
        async with http:
            revoked = await http.post("/mcp", json={}, headers=bearer(tokens["access_token"]))
            made_up = await http.post("/mcp", json={}, headers=bearer("jxo_made-up"))

    assert revoked.status_code == made_up.status_code == 401


@pytest.mark.anyio
async def test_a_token_for_another_resource_is_refused(app, agent, browser):
    _, tokens = connect(agent, browser, posts="read")
    with app.state.sessionmaker() as session, session.begin():
        session.execute(text("UPDATE oauth_grants SET resource = 'https://other.example/mcp'"))

    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC)
        async with http:
            response = await http.post("/mcp", json={}, headers=bearer(tokens["access_token"]))
    loaded = await app.state.oauth_provider.load_access_token(tokens["access_token"])

    assert response.status_code == 401
    # Refused by the SDK's resource check, which sees the grant's own resource.
    assert loaded.resource == "https://other.example/mcp"


@pytest.mark.anyio
async def test_an_api_token_does_not_open_mcp(app):
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(session, "agent", {"posts": "write"})

    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC)
        async with http:
            response = await http.post("/mcp", json={}, headers=bearer(secret))

    assert response.status_code == 401


@pytest.mark.anyio
async def test_tools_work_when_the_request_came_through_the_proxy(app, agent, browser):
    _, tokens = connect(agent, browser, posts="read")
    through_proxy = {**bearer(tokens["access_token"]), "x-forwarded-for": "198.51.100.7"}

    async with mcp_client(app, headers=through_proxy) as client:
        answer = await call(client, "list_posts")

    assert answer == ("ok", [])


@pytest.mark.anyio
async def test_each_call_runs_with_the_token_it_came_with(app, agent, browser):
    """A refreshed token keeps the session, and the old one, now dead, is never used again."""
    client_id, first = connect(agent, browser, posts="read")

    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=PUBLIC,
            headers=bearer(first["access_token"]),
        )
        async with http, Client(streamable_http_client(RESOURCE, http_client=http)) as client:
            before = await call(client, "list_posts")
            second = refresh(agent, client_id, first["refresh_token"]).json()
            http.headers.update(bearer(second["access_token"]))
            after = await call(client, "list_posts")

    assert before == after == ("ok", [])


@pytest.mark.anyio
async def test_another_host_is_refused(app, agent, browser):
    _, tokens = connect(agent, browser, posts="read")

    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://rebound.example",
            headers=bearer(tokens["access_token"]),
        )
        async with http:
            response = await http.post("/mcp", json={})

    assert response.status_code == 421


@pytest.mark.anyio
async def test_a_tool_call_does_not_block_the_event_loop(app, agent, browser, monkeypatch):
    """The catalog endpoint waits until /health answers on the same loop: if the tool held the
    loop, /health could not run and the endpoint would give up."""
    _, tokens = connect(agent, browser, posts="read")
    health_answered = threading.Event()

    class SlowCatalog(dict):
        def items(self):
            assert health_answered.wait(timeout=5), "the event loop was blocked"
            return super().items()

    monkeypatch.setattr(catalog, "CATALOG", SlowCatalog(catalog.CATALOG))

    async with mcp_client(app, headers=bearer(tokens["access_token"])) as client:
        results = {}

        async def call_tool():
            results["tool"] = await call(client, "get_catalog")

        async def ask_health():
            await anyio.sleep(0.2)
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
                results["health"] = (await http.get(f"{PUBLIC}/health")).status_code
            health_answered.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(call_tool)
            tasks.start_soon(ask_health)

    assert results["health"] == 200
    assert results["tool"][0] == "ok"


@pytest.mark.anyio
async def test_a_crash_in_process_is_logged_and_reaches_the_agent_as_a_500(
    app, agent, browser, monkeypatch, caplog
):
    _, tokens = connect(agent, browser, posts="read")

    class BrokenCatalog(dict):
        def items(self):
            raise RuntimeError("catalog exploded")

    monkeypatch.setattr(catalog, "CATALOG", BrokenCatalog())

    async with mcp_client(app, headers=bearer(tokens["access_token"])) as client:
        outcome, error = await call(client, "get_catalog")

    assert outcome == "error"
    assert (error["status"], error["code"]) == (500, "http_error")
    crash = [r for r in caplog.records if r.name == "jorgex_social.remote_mcp.server"]
    assert crash and crash[0].getMessage() == "in-process GET /v1/catalog failed"
    assert "catalog exploded" in caplog.text
    assert tokens["access_token"] not in caplog.text


def test_without_a_public_url_there_is_no_mcp(settings, clock):
    without = TestClient(create_app(replace(settings, public_url=None), clock))

    assert without.post("/mcp", json={}).status_code == 404
    assert without.post("/upload/anything", content=JPEG).status_code == 404


# Upload URLs


def new_url(agent, token):
    response = api(agent, token, "/v1/upload-urls", "POST")
    assert response.status_code == 201, response.text
    return response.json()["url"].removeprefix(PUBLIC)


def test_an_upload_url_takes_one_file_once(app, agent, browser):
    _, tokens = connect(agent, browser, posts="write")
    path = new_url(agent, tokens["access_token"])

    first = agent.post(path, content=JPEG)
    again = agent.post(path, content=JPEG)

    assert first.status_code == 201
    assert first.json()["type"] == "jpeg" and first.json()["bytes"] == len(JPEG)
    assert again.status_code == 404
    grant_id = rows(app, "SELECT id FROM oauth_grants")[0].id
    audited = rows(app, "SELECT actor_ref FROM audit_log WHERE action LIKE 'POST /upload%'")
    assert audited == [(f"oauth:{grant_id}",)]


def test_an_upload_url_lasts_ten_minutes(agent, browser, clock):
    _, tokens = connect(agent, browser, posts="write")
    path = new_url(agent, tokens["access_token"])
    clock.advance(minutes=10)

    assert agent.post(path, content=JPEG).status_code == 404


def test_an_upload_url_is_spent_even_when_the_file_is_refused(agent, browser):
    _, tokens = connect(agent, browser, posts="write")
    path = new_url(agent, tokens["access_token"])

    refused = agent.post(path, content=b"GIF89a not a photo")

    assert refused.status_code == 415
    assert agent.post(path, content=JPEG).status_code == 404


def test_an_upload_url_refuses_what_is_too_large(agent, browser):
    _, tokens = connect(agent, browser, posts="write")
    path = new_url(agent, tokens["access_token"])

    response = agent.post(path, content=JPEG, headers={"content-length": str(301 * 1024 * 1024)})

    assert response.status_code == 413


def test_only_the_url_opens_the_upload_route(app, agent, browser):
    _, tokens = connect(agent, browser, posts="write")
    with app.state.sessionmaker() as session, session.begin():
        _, api_secret = create_token(session, "agent", {"posts": "write"})

    for token in (tokens["access_token"], api_secret):
        response = agent.post("/upload/made-up", content=JPEG, headers=bearer(token))
        assert response.status_code == 404
    signed_in = browser.post("/upload/made-up", content=JPEG)
    assert signed_in.status_code == 404


def test_upload_urls_need_posts_write_and_an_oauth_grant(app, agent, browser):
    _, read_only = connect(agent, browser, posts="read")
    with app.state.sessionmaker() as session, session.begin():
        _, api_secret = create_token(session, "agent", {"posts": "write"})

    denied = api(agent, read_only["access_token"], "/v1/upload-urls", "POST")
    not_oauth = api(agent, api_secret, "/v1/upload-urls", "POST")

    assert denied.status_code == 403
    assert not_oauth.status_code == 400 and not_oauth.json()["detail"]["code"] == "oauth_only"


def test_revoking_the_grant_kills_its_upload_urls(agent, browser):
    client_id, tokens = connect(agent, browser, posts="write")
    path = new_url(agent, tokens["access_token"])

    revoke(agent, client_id, tokens["access_token"])

    assert agent.post(path, content=JPEG).status_code == 404


def test_upload_urls_are_limited_per_grant(agent, browser, clock):
    client_id, tokens = connect(agent, browser, posts="write")
    token = tokens["access_token"]
    paths = [new_url(agent, token) for _ in range(5)]

    sixth = api(agent, token, "/v1/upload-urls", "POST")
    assert sixth.status_code == 429
    assert sixth.json()["detail"]["code"] == "too_many_upload_urls"

    # Used ones free their place, but no more than twenty an hour.
    for path in paths:
        agent.post(path, content=JPEG)
    for _ in range(15):
        agent.post(new_url(agent, token), content=JPEG)
        clock.advance(seconds=1)
    assert api(agent, token, "/v1/upload-urls", "POST").status_code == 429
    clock.advance(hours=1)
    token = refresh(agent, client_id, tokens["refresh_token"]).json()["access_token"]
    assert api(agent, token, "/v1/upload-urls", "POST").status_code == 201


def test_the_upload_token_never_reaches_the_access_log(app, agent, browser, caplog):
    """Through a real uvicorn server, whose access log prints the path."""
    _, tokens = connect(agent, browser, posts="write")
    path = new_url(agent, tokens["access_token"])
    secret = path.rsplit("/", 1)[1]
    hide_query_strings()  # what `serve` does through logs.configure()
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off")
    )
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.01)
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=JPEG)
        with urllib.request.urlopen(request) as response:
            assert response.status == 201
    finally:
        server.should_exit = True
        thread.join(10)

    lines = [r.getMessage() for r in caplog.records if r.name == "uvicorn.access"]
    assert any('"POST /upload/[redacted] HTTP/1.1" 201' in line for line in lines)
    assert all(secret not in r.getMessage() for r in caplog.records)
    assert secret not in "\n".join(str(r) for r in rows(app, "SELECT * FROM upload_urls"))
