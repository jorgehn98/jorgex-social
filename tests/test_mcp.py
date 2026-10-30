import json
import logging
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError

import pytest
from mcp import Client

from jorgex_social.access.tokens import create_token, revoke_token
from jorgex_social.publishing import media as storage
from jorgex_social_mcp import client as http_client
from jorgex_social_mcp.client import Api, ApiError, check_url, urllib_transport
from jorgex_social_mcp.server import build_server, main

pytestmark = pytest.mark.anyio

BASE = "http://api.test"
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64
MP4 = b"\x00\x00\x00\x18ftypisom" + b"v" * 64
ALL_WRITE = {g: "write" for g in ("posts", "scheduling", "stats", "account", "rules", "events")}

READ_POSTS = {"get_catalog", "list_posts", "get_post"}
WRITE_POSTS = {"upload_media", "create_post", "update_post"}
SCHEDULING = {"schedule_post", "publish_now", "cancel_post", "resolve_post"}
STATS = {"account_stats", "media_stats", "media_history"}
READ_ACCOUNT = {"get_account", "list_incidents", "audit_log"}
WRITE_ACCOUNT = {"acknowledge_incident", "set_kill_switch"}
READ_RULES = {"list_rules", "get_rule", "test_rules"}
WRITE_RULES = {
    "create_rule",
    "update_rule",
    "activate_rule",
    "deactivate_rule",
    "archive_rule",
    "pause_conversation",
    "resume_conversation",
}
EVENTS = {"list_events", "list_deliveries", "list_conversations"}


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


async def call(client, name, /, **arguments):
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
        ({"rules": "read", "events": "read"}, READ_RULES | EVENTS),
        ({"rules": "write"}, READ_RULES | WRITE_RULES),
        (
            ALL_WRITE,
            READ_POSTS
            | WRITE_POSTS
            | SCHEDULING
            | STATS
            | READ_ACCOUNT
            | WRITE_ACCOUNT
            | READ_RULES
            | WRITE_RULES
            | EVENTS,
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
        ("GET", "/v1/catalog"),  # the upload limit, read once
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
    bodies = {path: json.loads(body) for _, path, _, body in transport.calls[2:] if body}
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


async def test_reply_tools_reach_their_endpoints_end_to_end(app, transport):
    server = server_for(app, transport, rules="write", events="read")
    content = {"keywords": ["price"], "private_replies": ["See the website."]}

    async with Client(server) as client:
        transport.calls.clear()
        _, rule = await call(
            client, "create_rule", name="price", channel="comment", content=content
        )
        rid = rule["id"]
        r = {
            "listed": await call(client, "list_rules", channel="comment", active=False),
            "detail": await call(client, "get_rule", rule_id=rid),
            "edited": await call(
                client, "update_rule", rule_id=rid, priority=5, clear=["media_ref"]
            ),
            "tested": await call(
                client, "test_rules", channel="comment", text="the PRICE?", rule_id=rid
            ),
            "active": await call(client, "activate_rule", rule_id=rid),
            "inactive": await call(client, "deactivate_rule", rule_id=rid),
            "archived": await call(client, "archive_rule", rule_id=rid),
            "events": await call(client, "list_events", matched=True, limit=5),
            "deliveries": await call(client, "list_deliveries", status="sent", reason="paused"),
            "conversations": await call(client, "list_conversations", paused=True),
            "pause": await call(client, "pause_conversation", conversation_id="c1", forever=True),
            "resume": await call(client, "resume_conversation", conversation_id="c1"),
        }

    sent = [(method, path) for method, path, _, _ in transport.calls]
    assert sent == [
        ("POST", "/v1/rules"),
        ("GET", "/v1/rules?channel=comment&active=false"),
        ("GET", f"/v1/rules/{rid}"),
        ("PATCH", f"/v1/rules/{rid}"),
        ("POST", "/v1/rules/test"),
        ("POST", f"/v1/rules/{rid}/activate"),
        ("POST", f"/v1/rules/{rid}/deactivate"),
        ("POST", f"/v1/rules/{rid}/archive"),
        ("GET", "/v1/events?matched=true&limit=5"),
        ("GET", "/v1/deliveries?status=sent&reason=paused"),
        ("GET", "/v1/conversations?paused=true"),
        ("POST", "/v1/conversations/c1/pause"),
        ("POST", "/v1/conversations/c1/resume"),
    ]
    bodies = {(m, path): json.loads(body) for m, path, _, body in transport.calls if body}
    assert bodies[("PATCH", f"/v1/rules/{rid}")] == {"media_ref": None, "priority": 5}
    assert bodies[("POST", "/v1/conversations/c1/pause")] == {"forever": True}

    assert [x["id"] for x in r["listed"][1]] == [rid]
    assert r["detail"][1]["content"]["keywords"] == ["price"]
    assert r["edited"][1]["priority"] == 5
    assert r["tested"][1]["private_replies"] == ["See the website."]
    assert (r["active"][1]["active"], r["inactive"][1]["active"]) == (True, False)
    assert r["archived"][1]["archived_at"] is not None
    assert r["events"] == r["deliveries"] == r["conversations"] == ("ok", [])
    assert r["pause"][1]["code"] == r["resume"][1]["code"] == "not_found"


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


@contextmanager
def loopback_server(status=200, body=b"{}", headers=()):
    """A real HTTP server on 127.0.0.1 that answers every GET the same way and counts them."""
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("token", ["jxs_abc\r", "jxs_abc\n", "jxs abc", "jxs_ñ"])
def test_a_malformed_token_never_leaks_from_the_real_transport(token):
    # urllib refuses the header before connecting, quoting it whole; nothing listens on port 9.
    with pytest.raises(ApiError) as error:
        Api("http://127.0.0.1:9", token, urllib_transport).request("GET", "/v1/token")

    assert error.value.payload["code"] in {"client_error", "unreachable"}
    assert token.strip() not in str(error.value)


def test_main_refuses_a_malformed_token(monkeypatch):
    token = "jxs_from_a_crlf_file\r"
    monkeypatch.setenv("JORGEX_SOCIAL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("JORGEX_SOCIAL_TOKEN", token)

    with pytest.raises(SystemExit) as exit_:
        main()

    assert "JORGEX_SOCIAL_TOKEN must be visible ASCII" in str(exit_.value.code)
    assert token.strip() not in str(exit_.value.code)


def test_the_token_never_goes_to_an_environment_proxy(monkeypatch):
    with (
        loopback_server(body=b'{"permissions": {}}') as (target, target_hits),
        loopback_server() as (proxy, proxy_hits),
    ):
        monkeypatch.setenv("http_proxy", proxy)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        # The opener reads the environment when built, so build it with the proxy set.
        monkeypatch.setattr(http_client, "_opener", http_client._make_opener())

        answer = Api(target, "jxs_token", urllib_transport).request("GET", "/v1/token")

    assert answer == {"permissions": {}}
    assert (target_hits, proxy_hits) == (["/v1/token"], [])


def test_a_redirect_is_an_error_not_an_answer():
    location = [("Location", "https://elsewhere.example.com/v1/token")]
    with loopback_server(302, b"", location) as (url, hits), pytest.raises(ApiError) as error:
        build_server(Api(url, "jxs_token", urllib_transport))

    assert hits == ["/v1/token"]
    assert error.value.payload["status"] == 302
    assert error.value.payload["code"] == "redirect"


@pytest.mark.parametrize("body", [b"[]", b'{"name": "x"}', b'{"permissions": ["posts"]}'])
def test_startup_needs_the_token_permissions(body):
    with loopback_server(body=body) as (url, _), pytest.raises(ApiError) as error:
        build_server(Api(url, "jxs_token", urllib_transport))

    assert error.value.payload["code"] == "invalid_response"


@pytest.mark.parametrize("post_id", ["", ".", ".."])
async def test_ids_that_walk_the_path_are_refused(app, transport, post_id):
    server = server_for(app, transport, posts="read")
    calls_before = len(transport.calls)

    async with Client(server) as client:
        outcome, error = await call(client, "get_post", post_id=post_id)

    assert (outcome, error["code"]) == ("error", "invalid_id")
    assert len(transport.calls) == calls_before


async def test_upload_over_the_server_limit_is_not_sent(app, transport, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "MAX_UPLOAD_BYTES", len(JPEG))
    (tmp_path / "fits.jpg").write_bytes(JPEG)
    (tmp_path / "big.jpg").write_bytes(JPEG + b"x")
    server = server_for(app, transport, posts="write")

    async with Client(server) as client:
        fits = await call(client, "upload_media", path=str(tmp_path / "fits.jpg"))
        big = await call(client, "upload_media", path=str(tmp_path / "big.jpg"))

    assert fits[0] == "ok"
    assert big == (
        "error",
        {
            "status": None,
            "code": "too_large",
            "message": f"Uploads are limited to {len(JPEG)} bytes.",
        },
    )
    assert [m for m, path, _, _ in transport.calls if path == "/v1/media"] == ["POST"]


async def test_update_refuses_a_field_both_given_and_cleared(app, transport):
    server = server_for(app, transport, posts="write")
    calls_before = len(transport.calls)

    async with Client(server) as client:
        outcome, error = await call(
            client, "update_post", post_id="p1", source_ref="mine", clear=["source_ref"]
        )

    assert (outcome, error["code"]) == ("error", "invalid_value")
    assert "source_ref" in error["message"]
    assert len(transport.calls) == calls_before
