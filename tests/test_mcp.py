import json
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from urllib.error import URLError

import pytest

pytest.importorskip("mcp", reason="install the mcp extra: uv sync --extra mcp")

from mcp import Client  # noqa: E402

from jorgex_social.access.tokens import create_token, revoke_token  # noqa: E402
from jorgex_social_mcp.client import Api, ApiError, check_url  # noqa: E402
from jorgex_social_mcp.server import build_server, main  # noqa: E402

pytestmark = pytest.mark.anyio

BASE = "http://api.test"
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64
MP4 = b"\x00\x00\x00\x18ftypisom" + b"v" * 64
ALL_WRITE = {g: "write" for g in ("posts", "scheduling", "stats", "account")}

READ_POSTS = {"get_catalog", "list_posts", "get_post"}
WRITE_POSTS = {"upload_media", "create_post", "update_post"}
SCHEDULING = {"schedule_post", "publish_now", "cancel_post", "resolve_post"}
STATS = {"account_stats", "media_stats", "media_history"}
READ_ACCOUNT = {"get_account", "list_incidents", "audit_log"}
WRITE_ACCOUNT = {"acknowledge_incident", "set_kill_switch"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Recorder:
    """Sends each request to the real app in process and remembers what went out."""

    def __init__(self, client):
        self.client = client
        self.calls = []

    def __call__(self, method, url, headers, body):
        assert url.startswith(BASE)
        content = body.read() if hasattr(body, "read") else body
        self.calls.append((method, url.removeprefix(BASE), headers, content))
        response = self.client.request(
            method, url.removeprefix(BASE), headers=headers, content=content
        )
        return response.status_code, response.content


@pytest.fixture
def transport(client):
    return Recorder(client)


def make_secret(app, name="agent", **grants):
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(session, name, grants)
    return secret


def server_for(app, transport, **grants):
    return build_server(Api(BASE, make_secret(app, **grants), transport))


async def tool_names(server):
    async with Client(server) as client:
        return {tool.name for tool in (await client.list_tools()).tools}


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    text = result.content[0].text if result.content else "null"
    if result.is_error:
        # The SDK prefixes "Error executing tool <name>: " to the API's error.
        prefix = f"Error executing tool {name}: "
        assert text.startswith(prefix), text
        return "error", json.loads(text.removeprefix(prefix))
    return "ok", json.loads(text)


@pytest.mark.parametrize(
    ("grants", "expected"),
    [
        ({}, set()),
        ({"posts": "read"}, READ_POSTS),
        ({"posts": "write"}, READ_POSTS | WRITE_POSTS),
        # scheduling has no read tools, so read exposes nothing.
        ({"scheduling": "read"}, set()),
        ({"scheduling": "write", "stats": "read"}, SCHEDULING | STATS),
        ({"account": "read", "stats": "write"}, READ_ACCOUNT | STATS),
        ({"account": "write"}, READ_ACCOUNT | WRITE_ACCOUNT),
        (
            ALL_WRITE,
            READ_POSTS | WRITE_POSTS | SCHEDULING | STATS | READ_ACCOUNT | WRITE_ACCOUNT,
        ),
    ],
)
async def test_tools_follow_the_token_permissions(app, transport, grants, expected):
    assert await tool_names(server_for(app, transport, **grants)) == expected


async def test_tools_reach_their_endpoints_end_to_end(app, transport, tmp_path):
    server = server_for(app, transport, **ALL_WRITE)
    photo = tmp_path / "photo.JPG"
    photo.write_bytes(JPEG)
    when = (datetime.now(UTC) + timedelta(days=1)).isoformat()

    async with Client(server) as client:
        transport.calls.clear()
        _, media = await call(client, "upload_media", path=str(photo))
        _, post = await call(
            client, "create_post", type="image", items=[media["id"]], caption="Hello #ai"
        )
        pid = post["id"]
        r = {
            "catalog": await call(client, "get_catalog"),
            "listed": await call(client, "list_posts", status="draft", limit=5),
            "detail": await call(client, "get_post", post_id=pid),
            "edited": await call(
                client, "update_post", post_id=pid, caption="Bye", clear=["source_ref"]
            ),
            "scheduled": await call(client, "schedule_post", post_id=pid, scheduled_for=when),
            "cancelled": await call(client, "cancel_post", post_id=pid),
            "account_stats": await call(client, "account_stats", limit=7),
            "media_stats": await call(client, "media_stats"),
            "history": await call(client, "media_history", media_ref="m1"),
            "account": await call(client, "get_account"),
            "incidents": await call(client, "list_incidents", open=True),
            "kill_switch": await call(client, "set_kill_switch", enabled=True),
            "audit": await call(client, "audit_log", object_type="post", object_id=pid),
        }
        _, draft = await call(client, "create_post", type="image", items=[media["id"]], caption="x")
        r["now"] = await call(client, "publish_now", post_id=draft["id"])
        r["resolve"] = await call(client, "resolve_post", post_id=draft["id"], outcome="published")
        r["ack"] = await call(client, "acknowledge_incident", incident_id="nope")

    sent = [(method, path) for method, path, _, _ in transport.calls]
    assert sent == [
        ("POST", "/v1/media"),
        ("POST", "/v1/posts"),
        ("GET", "/v1/catalog"),
        ("GET", "/v1/posts?status=draft&limit=5"),
        ("GET", f"/v1/posts/{pid}"),
        ("PATCH", f"/v1/posts/{pid}"),
        ("POST", f"/v1/posts/{pid}/schedule"),
        ("POST", f"/v1/posts/{pid}/cancel"),
        ("GET", "/v1/stats/account?limit=7"),
        ("GET", "/v1/stats/media"),
        ("GET", "/v1/stats/media/m1"),
        ("GET", "/v1/account"),
        ("GET", "/v1/account/incidents?open=true"),
        ("PUT", "/v1/account/kill-switch"),
        ("GET", f"/v1/account/audit?object_type=post&object_id={pid}"),
        ("POST", "/v1/posts"),
        ("POST", f"/v1/posts/{draft['id']}/publish-now"),
        ("POST", f"/v1/posts/{draft['id']}/resolve"),
        ("POST", "/v1/account/incidents/nope/acknowledge"),
    ]
    bodies = {path: json.loads(body) for _, path, _, body in transport.calls[1:] if body}
    assert bodies[f"/v1/posts/{pid}"] == {"source_ref": None, "caption": "Bye"}
    assert bodies[f"/v1/posts/{pid}/schedule"] == {"scheduled_for": when}
    assert bodies["/v1/account/kill-switch"] == {"enabled": True}

    assert r["catalog"][1]["upload"]["max_bytes"] > 0
    assert [p["id"] for p in r["listed"][1]] == [pid]
    assert r["detail"][1]["attempts"] == []
    assert r["edited"][1]["caption"] == "Bye"
    assert r["scheduled"][1]["status"] == "scheduled"
    assert r["cancelled"][1]["status"] == "cancelled"
    assert r["account_stats"] == r["media_stats"] == r["history"] == ("ok", [])
    assert r["account"][1]["kill_switch"] is False
    assert r["incidents"] == ("ok", [])
    assert r["kill_switch"][1]["enabled"] is True
    assert r["audit"][1] and {e["object_id"] for e in r["audit"][1]} == {pid}
    assert r["now"][1]["status"] == "scheduled"
    assert (r["resolve"][0], r["resolve"][1]["code"]) == ("error", "invalid_state")
    assert r["ack"] == (
        "error",
        {"status": 404, "code": "not_found", "message": "No such incident."},
    )


async def test_api_errors_come_back_as_they_are(app, transport):
    server = server_for(app, transport, posts="read")

    async with Client(server) as client:
        outcome, error = await call(client, "get_post", post_id="nope")
        _, invalid = await call(client, "list_posts", limit=0)

    assert outcome == "error"
    assert error == {"status": 404, "code": "not_found", "message": "No such post."}
    assert invalid["status"] == 422 and invalid["code"] == "http_error"


async def test_ids_cannot_reach_another_endpoint(app, transport):
    server = server_for(app, transport, posts="read")

    async with Client(server) as client:
        outcome, error = await call(client, "get_post", post_id="../catalog")

    assert transport.calls[-1][1] == "/v1/posts/..%2Fcatalog"
    assert (outcome, error["status"]) == ("error", 404)


@pytest.mark.parametrize(
    ("name", "body", "content_type"),
    [
        ("a.jpeg", JPEG, "image/jpeg"),
        ("a.mp4", MP4, "video/mp4"),
        ("a.MOV", b"\x00\x00\x00\x14ftypqt  " + b"q" * 64, "video/quicktime"),
    ],
)
async def test_upload_sends_the_file_raw_with_its_type(
    app, transport, tmp_path, name, body, content_type
):
    (tmp_path / name).write_bytes(body)
    (tmp_path / "other.jpg").write_bytes(JPEG + b"other")
    server = server_for(app, transport, posts="write")

    async with Client(server) as client:
        outcome, media = await call(client, "upload_media", path=str(tmp_path / name))

    method, path, headers, sent = transport.calls[-1]
    assert (outcome, method, path) == ("ok", "POST", "/v1/media")
    assert headers["Content-Type"] == content_type
    assert headers["Content-Length"] == str(len(body))
    assert sent == body
    assert media["bytes"] == len(body)


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("relative/photo.jpg", "invalid_path"),
        ("{tmp}/photo.png", "unsupported_media_type"),
        ("{tmp}/folder.jpg", "not_a_file"),
        ("{tmp}/missing.jpg", "not_a_file"),
    ],
)
async def test_upload_refuses_what_it_should_not_read(app, transport, tmp_path, path, code):
    (tmp_path / "photo.png").write_bytes(b"\x89PNG")
    (tmp_path / "folder.jpg").mkdir()
    server = server_for(app, transport, posts="write")
    calls_before = len(transport.calls)

    async with Client(server) as client:
        outcome, error = await call(client, "upload_media", path=path.format(tmp=tmp_path))

    assert (outcome, error["code"]) == ("error", code)
    assert len(transport.calls) == calls_before


async def test_the_token_never_shows(app, transport, caplog):
    caplog.set_level(logging.DEBUG)
    secret = make_secret(app, **ALL_WRITE)
    offline = False

    def flaky(method, url, headers, body):
        if offline:
            raise URLError("connection refused")
        return transport(method, url, headers, body)

    server = build_server(Api(BASE, secret, flaky))
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        _, denied = await call(client, "get_post", post_id="nope")
        offline = True
        _, unreachable = await call(client, "get_account")
    with app.state.sessionmaker() as session, session.begin():
        revoke_token(session, "agent")
    with pytest.raises(ApiError) as revoked:
        build_server(Api(BASE, secret, transport))

    assert denied["code"] == "not_found"
    assert unreachable["code"] == "unreachable"
    assert revoked.value.payload["code"] == "invalid_token"
    surfaces = [
        json.dumps([t.model_dump(mode="json") for t in tools]),
        json.dumps([denied, unreachable]),
        str(revoked.value),
        caplog.text,
    ]
    for surface in surfaces:
        assert secret not in surface


@pytest.mark.parametrize(
    "url",
    [
        "https://social.example.com",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://127.8.9.10",
        "http://[::1]:8000/",
    ],
)
def test_url_is_https_or_loopback(url):
    check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://social.example.com",
        "http://192.168.1.10:8000",
        "http://localhost.example.com",
        "http://127.0.0.1.example.com",
        "ftp://localhost",
        "social.example.com",
        "https://",
    ],
)
def test_main_refuses_a_url_that_would_send_the_token_in_the_clear(monkeypatch, url):
    secret = "jxs_secret_value"
    monkeypatch.setenv("JORGEX_SOCIAL_URL", url)
    monkeypatch.setenv("JORGEX_SOCIAL_TOKEN", secret)

    def no_network(_api):
        raise AssertionError("main must stop before any request")

    monkeypatch.setattr("jorgex_social_mcp.server.build_server", no_network)

    with pytest.raises(SystemExit) as exit_:
        main()

    assert "https" in str(exit_.value.code)
    assert secret not in str(exit_.value.code)


def test_the_service_does_not_import_mcp():
    code = (
        "import sys, jorgex_social.api, jorgex_social.cli, jorgex_social.worker; "
        "sys.exit('mcp' in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
