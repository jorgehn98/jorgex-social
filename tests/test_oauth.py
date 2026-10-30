import base64
import hashlib
import html
import json
import logging
import re
import secrets
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pyotp
import pytest
from fastapi.testclient import TestClient
from mcp.client.auth import OAuthClientProvider
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
from sqlalchemy import select, text

from jorgex_social.access import operator
from jorgex_social.account import crypto
from jorgex_social.api import create_app
from jorgex_social.cli import main
from jorgex_social.clock import ManualClock
from jorgex_social.config import Settings
from jorgex_social.oauth import store
from jorgex_social.oauth.models import OAuthGrant, OAuthToken
from jorgex_social.ui.routes import COOKIE

PUBLIC = "http://localhost"
RESOURCE = f"{PUBLIC}/mcp"
REDIRECT = "http://127.0.0.1:33418/callback"
START = datetime(2026, 10, 30, 12, 0, 10, tzinfo=UTC)
PASSWORD = "correct horse battery staple"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
ALL_DENIED = {g: "deny" for g in ("posts", "scheduling", "rules", "events", "stats", "account")}


@pytest.fixture
def clock():
    return ManualClock(START)


@pytest.fixture
def settings(settings):
    return replace(settings, public_url=PUBLIC)


@pytest.fixture
def app(settings, clock):
    key = crypto.load_key(settings.secret_key_file)
    app = create_app(settings, clock)
    earlier = clock.now() - timedelta(seconds=30)
    with app.state.sessionmaker() as session, session.begin():
        operator.create_operator(session, PASSWORD, earlier)
        operator.enroll_totp(
            session,
            app.state.network,
            key,
            TOTP_SECRET,
            pyotp.TOTP(TOTP_SECRET).at(earlier),
            earlier,
        )
    return app


@pytest.fixture
def agent(app) -> TestClient:
    """The MCP client: no cookies, no Origin."""
    return TestClient(app, base_url=PUBLIC)


@pytest.fixture
def browser(app, clock) -> TestClient:
    """The operator's browser, signed in."""
    browser = TestClient(app, base_url=PUBLIC, headers={"origin": PUBLIC})
    sign_in(browser, clock)
    return browser


def sign_in(browser, clock):
    response = browser.post(
        "/ui/login",
        data={"password": PASSWORD, "code": pyotp.TOTP(TOTP_SECRET).at(clock.now())},
        follow_redirects=False,
    )
    assert response.status_code == 303
    clock.advance(seconds=30)  # each sign-in needs a fresh TOTP step


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def register(agent, **metadata):
    body = {
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        "client_name": "Test agent",
        **metadata,
    }
    return agent.post("/register", json=body)


def start(agent, client_id, challenge, **params):
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "the-state",
        "resource": RESOURCE,
        **params,
    }
    query = {name: value for name, value in query.items() if value is not None}
    return agent.get("/authorize", params=query, follow_redirects=False)


def open_consent(browser, location):
    page = browser.get(location, follow_redirects=False)
    fields = dict(re.findall(r'name="(req|nonce)" value="([^"]+)"', page.text))
    return page, fields


def answer(browser, fields, decision="allow", **levels):
    return browser.post(
        "/ui/oauth/consent",
        data={**fields, "decision": decision, **levels},
        follow_redirects=False,
    )


def query_of(response) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}


def exchange(agent, client_id, code, verifier, redirect_uri=REDIRECT):
    return agent.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": RESOURCE,
        },
    )


def refresh(agent, client_id, refresh_token, **extra):
    return agent.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            **extra,
        },
    )


def code_for(agent, browser, client_id=None, **levels):
    """Registers (unless given a client) and runs the browser part. Returns the client id, the
    PKCE verifier and the code."""
    client_id = client_id or register(agent).json()["client_id"]
    verifier, challenge = pkce()
    _, fields = open_consent(browser, start(agent, client_id, challenge).headers["location"])
    code = query_of(answer(browser, fields, **levels))["code"]
    return client_id, verifier, code


def connect(agent, browser, **levels) -> tuple[str, dict]:
    client_id, verifier, code = code_for(agent, browser, **levels)
    tokens = exchange(agent, client_id, code, verifier)
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


def api(agent, token, path="/v1/token", method="GET", **kwargs):
    return agent.request(method, path, headers={"authorization": f"Bearer {token}"}, **kwargs)


def rows(app, query, **params):
    with app.state.read_sessionmaker() as session, session.begin():
        return session.execute(text(query), params).all()


def audit_actions(app) -> list[tuple[str, str]]:
    return [
        tuple(r)
        for r in rows(
            app, "SELECT action, outcome FROM audit_log WHERE action LIKE 'oauth.%' ORDER BY rowid"
        )
    ]


# The whole flow, as an MCP client does it


class MemoryStorage:
    def __init__(self):
        self.tokens = self.client_info = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client_info

    async def set_client_info(self, client_info):
        self.client_info = client_info


async def operator_oauth(transport, clock, **levels):
    """An MCP client's OAuth as the SDK does it, with the operator signed in on the consent page
    granting `levels`. Returns the httpx auth, its storage and what the browser saw."""
    browser = httpx2.AsyncClient(transport=transport, base_url=PUBLIC, headers={"origin": PUBLIC})
    code = pyotp.TOTP(TOTP_SECRET).at(clock.now())
    signed_in = await browser.post("/ui/login", data={"password": PASSWORD, "code": code})
    assert signed_in.status_code == 303
    seen = {}

    async def open_in_browser(authorization_url: str) -> None:
        # What the operator does: the consent page, choosing levels, Allow.
        to_consent = await browser.get(authorization_url)
        page = await browser.get(to_consent.headers["location"])
        seen["page"] = page
        fields = dict(re.findall(r'name="(req|nonce)" value="([^"]+)"', page.text))
        seen.update(fields)
        seen["back"] = await browser.post(
            "/ui/oauth/consent", data={**fields, "decision": "allow", **levels}
        )
        await browser.aclose()

    async def receive_callback() -> AuthorizationCodeResult:
        back = query_of(seen["back"])
        seen["code"] = back["code"]
        return AuthorizationCodeResult(code=back["code"], state=back["state"])

    storage = MemoryStorage()
    oauth = OAuthClientProvider(
        server_url=RESOURCE,
        client_metadata=OAuthClientMetadata(
            client_name="Test agent",
            redirect_uris=[REDIRECT],
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=open_in_browser,
        callback_handler=receive_callback,
    )
    return oauth, storage, seen


@pytest.mark.anyio
async def test_an_mcp_client_connects_with_only_the_url(app, clock, settings, caplog):
    caplog.set_level(logging.DEBUG)
    transport = httpx2.ASGITransport(app=app)
    oauth, storage, seen = await operator_oauth(transport, clock, posts="write", stats="read")
    async with httpx2.AsyncClient(transport=transport, base_url=PUBLIC, auth=oauth) as agent:
        response = await agent.get("/v1/token")

    assert response.status_code == 200
    assert response.json()["permissions"] == {**ALL_DENIED, "posts": "write", "stats": "read"}
    assert seen["back"].status_code == 303
    assert "Test agent" in seen["page"].text and "127.0.0.1:33418" in seen["page"].text
    assert storage.client_info.client_secret is None
    # Codes, tokens and the consent values are nowhere: not in the database, not in the logs.
    secrets_seen = [
        storage.tokens.access_token,
        storage.tokens.refresh_token,
        seen["code"],
        seen["req"],
        seen["nonce"],
    ]
    dump = "\n".join(sqlite3.connect(settings.database_path).iterdump())
    # The test's own HTTP client logs every URL it requests; the server's loggers are what count.
    server_log = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    for secret in secrets_seen:
        assert secret not in dump
        assert secret not in server_log
    assert audit_actions(app) == [("oauth.consent", "ok"), ("oauth.token_issued", "ok")]


# Metadata and registration


def test_metadata_announces_public_clients_and_our_revocation(agent):
    server = agent.get("/.well-known/oauth-authorization-server").json()
    resource = agent.get("/.well-known/oauth-protected-resource/mcp").json()

    assert server["token_endpoint_auth_methods_supported"] == ["none"]
    assert server["revocation_endpoint"] == f"{PUBLIC}/revoke"
    assert server["revocation_endpoint_auth_methods_supported"] == ["none"]
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert server["registration_endpoint"] == f"{PUBLIC}/register"
    assert resource["resource"] == RESOURCE


@pytest.mark.parametrize(
    "metadata",
    [
        {"token_endpoint_auth_method": "client_secret_post"},
        {"token_endpoint_auth_method": "client_secret_basic"},
        {"redirect_uris": ["http://attacker.example/callback"]},
        {"redirect_uris": ["https://attacker.example/callback"]},
        {"redirect_uris": ["http://localhost.attacker.example:1234/callback"]},
        {"redirect_uris": ["http://user@127.0.0.1:1234/callback"]},
        {"redirect_uris": [REDIRECT, "http://attacker.example/callback"]},
        {"grant_types": ["authorization_code", "client_credentials"]},
        {"client_name": "x" * 101},
    ],
)
def test_registration_refuses_anything_but_a_public_loopback_client(app, agent, metadata):
    assert register(agent, **metadata).status_code == 400
    assert rows(app, "SELECT id FROM oauth_clients") == []


def test_registration_without_an_auth_method_is_not_a_public_client(app, agent):
    # The SDK would register it as client_secret_post and mint a secret.
    response = agent.post("/register", json={"redirect_uris": [REDIRECT]})

    assert response.status_code == 400
    assert rows(app, "SELECT id FROM oauth_clients") == []


@pytest.mark.parametrize(
    "uri", [REDIRECT, "http://localhost:51000/callback", "http://[::1]:51000/callback"]
)
def test_loopback_redirects_are_registered_without_a_secret(agent, uri):
    response = register(agent, redirect_uris=[uri])

    assert response.status_code == 201
    assert "client_secret" not in response.json()


@pytest.mark.parametrize(
    ("listed", "registered"),
    [
        ("https://client.example/callback", "https://client.example/callback"),
        ("HTTPS://Client.Example:443/callback", "https://client.example/callback"),
        ("https://client.example", "https://client.example"),
    ],
)
def test_a_listed_https_redirect_is_accepted(settings, clock, listed, registered):
    configured = Settings.from_env({"JORGEX_SOCIAL_OAUTH_REDIRECT_URIS": listed})
    allowed = replace(settings, oauth_redirect_uris=configured.oauth_redirect_uris)
    agent = TestClient(create_app(allowed, clock))

    assert register(agent, redirect_uris=[registered]).status_code == 201
    assert register(agent, redirect_uris=["https://client.example/other"]).status_code == 400


def test_a_client_nobody_approved_is_forgotten_after_an_hour(app, agent, clock):
    client_id = register(agent).json()["client_id"]
    clock.advance(minutes=61)

    response = start(agent, client_id, pkce()[1])

    assert response.status_code == 400
    assert "location" not in response.headers
    register(agent)  # registering purges it
    assert client_id not in [r.id for r in rows(app, "SELECT id FROM oauth_clients")]


def test_registration_body_is_capped(agent):
    response = register(agent, client_uri="https://client.example/" + "x" * 20_000)

    assert response.status_code == 413


def test_registration_is_limited_per_address(agent):
    statuses = [register(agent).status_code for _ in range(11)]

    assert statuses == [201] * 10 + [429]


# Authorization request


def test_an_unregistered_redirect_uri_is_an_error_never_a_redirect(agent):
    client_id = register(agent).json()["client_id"]

    response = start(agent, client_id, pkce()[1], redirect_uri="http://127.0.0.1:33419/callback")

    assert response.status_code == 400
    assert "location" not in response.headers


@pytest.mark.parametrize("resource", ["https://other.example/mcp", f"{PUBLIC}/v1", None])
def test_a_request_for_another_resource_is_refused(app, agent, resource):
    client_id = register(agent).json()["client_id"]

    response = start(agent, client_id, pkce()[1], resource=resource)

    assert query_of(response)["error"] == "invalid_target"
    assert rows(app, "SELECT id_hash FROM oauth_requests") == []


def test_pkce_plain_is_refused(app, agent):
    client_id = register(agent).json()["client_id"]

    response = start(agent, client_id, "a" * 43, code_challenge_method="plain")

    assert query_of(response)["error"] == "invalid_request"
    assert rows(app, "SELECT id_hash FROM oauth_requests") == []


# Consent


def test_consent_needs_the_operator_signed_in(agent):
    client_id = register(agent).json()["client_id"]
    location = start(agent, client_id, pkce()[1]).headers["location"]
    stranger = TestClient(agent.app, base_url=PUBLIC)

    response = stranger.get(location, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/login?next=")


def test_consent_works_with_a_sign_in_under_an_hour_old(agent, browser, clock):
    clock.advance(minutes=58, seconds=30)  # sign_in already advanced 30 s
    client_id = register(agent).json()["client_id"]
    location = start(agent, client_id, pkce()[1]).headers["location"]

    page, fields = open_consent(browser, location)

    assert page.status_code == 200
    assert answer(browser, fields).headers["location"].startswith(REDIRECT)


def test_consent_with_an_old_sign_in_asks_for_credentials_and_then_finishes(agent, browser, clock):
    clock.advance(minutes=61)
    client_id = register(agent).json()["client_id"]
    verifier, challenge = pkce()
    location = start(agent, client_id, challenge).headers["location"].removeprefix(PUBLIC)
    old_cookie = browser.cookies[COOKIE]

    response = browser.get(location, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login?" + urlencode({"next": location})
    assert "set-cookie" not in response.headers
    assert browser.cookies[COOKIE] == old_cookie
    assert browser.get("/ui/", follow_redirects=False).status_code == 200

    # The sign-in form opens with the old session still valid, and carries the page to return to.
    form = browser.get(response.headers["location"], follow_redirects=False)
    assert form.status_code == 200
    next_page = html.unescape(re.search(r'name="next" value="([^"]+)"', form.text)[1])
    signed_in = browser.post(
        "/ui/login",
        data={
            "password": PASSWORD,
            "code": pyotp.TOTP(TOTP_SECRET).at(clock.now()),
            "next": next_page,
        },
        follow_redirects=False,
    )
    assert signed_in.headers["location"] == location
    assert browser.cookies[COOKIE] != old_cookie

    # The redirect bound nothing to the old session, so the new one can open and answer it.
    page, fields = open_consent(browser, signed_in.headers["location"])
    assert page.status_code == 200
    code = query_of(answer(browser, fields, posts="write"))["code"]
    assert exchange(agent, client_id, code, verifier).status_code == 200


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_an_old_sign_in_decides_nothing(app, agent, browser, clock, decision):
    clock.advance(minutes=55)
    client_id = register(agent).json()["client_id"]
    _, fields = open_consent(browser, start(agent, client_id, pkce()[1]).headers["location"])
    clock.advance(minutes=6)  # the request is still open; the sign-in is no longer recent

    response = answer(browser, fields, decision)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "set-cookie" not in response.headers
    assert browser.get("/ui/", follow_redirects=False).status_code == 200
    assert audit_actions(app) == []
    assert rows(app, "SELECT count(*) FROM oauth_codes") == [(0,)]
    assert rows(app, "SELECT count(*) FROM oauth_grants") == [(0,)]


def test_the_consent_page_shows_the_client_as_unverified_and_allows_only_its_origin(agent, browser):
    client_id = register(agent, client_name="Totally Official").json()["client_id"]
    location = start(agent, client_id, pkce()[1]).headers["location"]

    page, fields = open_consent(browser, location)

    assert page.status_code == 200
    assert "Totally Official" in page.text and "unverified" in page.text
    assert "127.0.0.1:33418" in page.text
    assert set(fields) == {"req", "nonce"}
    csp = page.headers["content-security-policy"]
    assert "form-action 'self' http://127.0.0.1:33418;" in csp
    assert "form-action 'self';" in browser.get("/ui/").headers["content-security-policy"]


def test_the_answer_is_a_303_with_the_code_and_state(agent, browser):
    client_id = register(agent).json()["client_id"]
    _, fields = open_consent(browser, start(agent, client_id, pkce()[1]).headers["location"])

    response = answer(browser, fields, posts="read")

    assert response.status_code == 303
    assert response.headers["location"].startswith(REDIRECT + "?")
    assert query_of(response)["state"] == "the-state"
    assert "code" in query_of(response)


def test_denying_sends_access_denied(app, agent, browser):
    client_id = register(agent).json()["client_id"]
    _, fields = open_consent(browser, start(agent, client_id, pkce()[1]).headers["location"])

    response = answer(browser, fields, decision="deny")

    assert response.status_code == 303
    assert query_of(response) == {"error": "access_denied", "state": "the-state"}
    assert audit_actions(app) == [("oauth.consent", "denied")]


def test_a_request_is_answered_once(agent, browser):
    client_id = register(agent).json()["client_id"]
    location = start(agent, client_id, pkce()[1]).headers["location"]
    _, fields = open_consent(browser, location)
    answer(browser, fields)

    again = answer(browser, fields)
    reopened, _ = open_consent(browser, location)

    assert again.status_code == 400 and "location" not in again.headers
    assert reopened.status_code == 400


def test_a_request_belongs_to_the_session_that_opened_it(app, agent, browser, clock):
    client_id = register(agent).json()["client_id"]
    location = start(agent, client_id, pkce()[1]).headers["location"]
    _, fields = open_consent(browser, location)
    other = TestClient(app, base_url=PUBLIC, headers={"origin": PUBLIC})
    sign_in(other, clock)

    assert open_consent(other, location)[0].status_code == 400
    response = answer(other, fields)

    assert response.status_code == 400 and "location" not in response.headers
    assert answer(browser, fields).status_code == 303  # still the first one's to answer


def test_a_request_expires_after_ten_minutes(agent, browser, clock):
    client_id = register(agent).json()["client_id"]
    location = start(agent, client_id, pkce()[1]).headers["location"]
    _, fields = open_consent(browser, location)
    clock.advance(minutes=10)

    assert answer(browser, fields).status_code == 400


def test_only_the_last_form_shown_can_answer(agent, browser):
    client_id = register(agent).json()["client_id"]
    location = start(agent, client_id, pkce()[1]).headers["location"]
    _, first = open_consent(browser, location)
    _, second = open_consent(browser, location)

    assert answer(browser, first).status_code == 400
    assert answer(browser, second).status_code == 303


def test_a_cross_site_answer_is_refused(app, agent, browser):
    client_id = register(agent).json()["client_id"]
    _, fields = open_consent(browser, start(agent, client_id, pkce()[1]).headers["location"])

    response = browser.post(
        "/ui/oauth/consent",
        data={**fields, "decision": "allow", "posts": "write"},
        headers={"origin": "https://attacker.example"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert rows(app, "SELECT hash FROM oauth_codes") == []


def test_an_unknown_level_is_refused(agent, browser):
    client_id = register(agent).json()["client_id"]
    _, fields = open_consent(browser, start(agent, client_id, pkce()[1]).headers["location"])

    assert answer(browser, fields, posts="admin").status_code == 400


# Codes


def test_a_code_works_once_and_a_second_use_ends_its_tokens(app, agent, browser):
    client_id, verifier, code = code_for(agent, browser, posts="read")
    tokens = exchange(agent, client_id, code, verifier).json()

    again = exchange(agent, client_id, code, verifier)

    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    assert api(agent, tokens["access_token"]).status_code == 401
    assert refresh(agent, client_id, tokens["refresh_token"]).status_code == 400
    assert ("oauth.revoked", "ok") in audit_actions(app)


def test_a_code_expires_after_a_minute(agent, browser, clock):
    client_id, verifier, code = code_for(agent, browser)
    clock.advance(seconds=61)

    assert exchange(agent, client_id, code, verifier).json()["error"] == "invalid_grant"


def test_a_code_needs_its_verifier_redirect_and_client(agent, browser):
    client_id, verifier, code = code_for(agent, browser)
    other = register(agent).json()["client_id"]

    assert exchange(agent, client_id, code, pkce()[0]).json()["error"] == "invalid_grant"
    wrong_redirect = exchange(agent, client_id, code, verifier, "http://127.0.0.1:1/callback")
    assert wrong_redirect.status_code == 400
    assert exchange(agent, other, code, verifier).json()["error"] == "invalid_grant"
    assert exchange(agent, client_id, code, verifier).status_code == 200


# What a token allows


def test_the_api_allows_exactly_what_was_granted(agent, browser):
    _, tokens = connect(agent, browser, posts="read", account="write")
    access = tokens["access_token"]

    assert api(agent, access, "/v1/posts").status_code == 200
    assert api(agent, access, "/v1/posts", "POST", json={}).status_code == 403
    assert api(agent, access, "/v1/stats/account").status_code == 403
    killed = api(agent, access, "/v1/account/kill-switch", "PUT", json={"enabled": True})
    assert killed.status_code == 200


def test_a_client_cannot_ask_for_more_than_was_granted(agent, browser):
    client_id = register(agent, scope="account:write posts:write").json()["client_id"]
    verifier, challenge = pkce()
    location = start(agent, client_id, challenge, scope="account:write").headers["location"]
    _, fields = open_consent(browser, location)
    code = query_of(answer(browser, fields, posts="read"))["code"]
    tokens = exchange(agent, client_id, code, verifier).json()

    assert api(agent, tokens["access_token"]).json()["permissions"] == {
        **ALL_DENIED,
        "posts": "read",
    }
    widened = refresh(agent, client_id, tokens["refresh_token"], scope="account:write")
    assert widened.json()["error"] == "invalid_scope"


def test_writes_with_an_oauth_token_are_audited_under_its_grant(app, agent, browser):
    _, tokens = connect(agent, browser, account="write")

    api(agent, tokens["access_token"], "/v1/account/kill-switch", "PUT", json={"enabled": True})

    grant_id = rows(app, "SELECT id FROM oauth_grants")[0].id
    entry = rows(app, "SELECT actor_type, actor_ref FROM audit_log WHERE action LIKE 'PUT%'")
    assert entry == [("token", f"oauth:{grant_id}")]


def test_an_access_token_lasts_an_hour(agent, browser, clock):
    _, tokens = connect(agent, browser, posts="read")
    clock.advance(minutes=59)
    assert api(agent, tokens["access_token"]).status_code == 200

    clock.advance(minutes=1)

    assert api(agent, tokens["access_token"]).status_code == 401


def test_tokens_are_bound_to_this_servers_resource(settings, clock, app, agent, browser):
    _, tokens = connect(agent, browser, posts="read")
    moved = TestClient(create_app(replace(settings, public_url="https://moved.example"), clock))
    without_oauth = TestClient(create_app(replace(settings, public_url=None), clock))

    assert api(moved, tokens["access_token"]).status_code == 401
    assert api(without_oauth, tokens["access_token"]).status_code == 401
    assert without_oauth.get("/.well-known/oauth-authorization-server").status_code == 404


# Refresh


def test_refresh_rotates_both_tokens(app, agent, browser, clock):
    client_id, first = connect(agent, browser, posts="read")
    clock.advance(minutes=50)

    second = refresh(agent, client_id, first["refresh_token"]).json()

    assert api(agent, second["access_token"]).status_code == 200
    assert api(agent, first["access_token"]).status_code == 401
    assert second["refresh_token"] != first["refresh_token"]
    assert refresh(agent, client_id, second["refresh_token"]).status_code == 200
    assert ("oauth.token_refreshed", "ok") in audit_actions(app)


def test_a_refresh_token_lasts_thirty_days(agent, browser, clock):
    client_id, tokens = connect(agent, browser)
    clock.advance(days=30)

    assert refresh(agent, client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_a_replayed_refresh_token_ends_the_whole_family(app, agent, browser, clock):
    client_id, first = connect(agent, browser, posts="read")
    second = refresh(agent, client_id, first["refresh_token"]).json()
    clock.advance(seconds=31)

    replay = refresh(agent, client_id, first["refresh_token"])

    assert replay.json()["error"] == "invalid_grant"
    assert api(agent, second["access_token"]).status_code == 401
    assert refresh(agent, client_id, second["refresh_token"]).status_code == 400
    assert ("oauth.refresh_reused", "denied") in audit_actions(app)


def test_a_retry_within_thirty_seconds_replaces_the_lost_pair(app, agent, browser, clock):
    client_id, first = connect(agent, browser, posts="read")
    lost = refresh(agent, client_id, first["refresh_token"]).json()
    clock.advance(seconds=29)

    retried = refresh(agent, client_id, first["refresh_token"])

    assert retried.status_code == 200
    assert api(agent, retried.json()["access_token"]).status_code == 200
    assert api(agent, lost["access_token"]).status_code == 401
    assert ("oauth.refresh_reused", "denied") not in audit_actions(app)


def test_a_retry_cannot_silently_take_the_family_over(app, agent, browser, clock):
    """If the retry came from a thief, the client's own refresh token, presented later, must end
    the grant instead of having quietly vanished."""
    client_id, first = connect(agent, browser, posts="read")
    clients_pair = refresh(agent, client_id, first["refresh_token"]).json()
    clock.advance(seconds=10)
    thiefs_pair = refresh(agent, client_id, first["refresh_token"]).json()
    clock.advance(seconds=31)

    replay = refresh(agent, client_id, clients_pair["refresh_token"])

    assert replay.json()["error"] == "invalid_grant"
    assert api(agent, thiefs_pair["access_token"]).status_code == 401
    assert ("oauth.refresh_reused", "denied") in audit_actions(app)


def test_the_pair_a_retry_replaced_ends_the_grant_even_within_the_grace(app, agent, browser):
    """The client rotated; a thief retried the old token; the client, its access now refused,
    refreshes at once. That is two holders: the family ends, the thief's branch included."""
    client_id, first = connect(agent, browser, posts="read")
    clients_pair = refresh(agent, client_id, first["refresh_token"]).json()
    thiefs_pair = refresh(agent, client_id, first["refresh_token"]).json()

    replay = refresh(agent, client_id, clients_pair["refresh_token"])

    assert replay.json()["error"] == "invalid_grant"
    assert api(agent, thiefs_pair["access_token"]).status_code == 401
    assert refresh(agent, client_id, thiefs_pair["refresh_token"]).status_code == 400
    assert ("oauth.refresh_reused", "denied") in audit_actions(app)


def test_only_one_retry_is_tolerated(agent, browser, clock):
    client_id, first = connect(agent, browser, posts="read")
    refresh(agent, client_id, first["refresh_token"])
    retried = refresh(agent, client_id, first["refresh_token"]).json()

    again = refresh(agent, client_id, first["refresh_token"])

    assert again.json()["error"] == "invalid_grant"
    assert api(agent, retried["access_token"]).status_code == 401


# A replay found in the exchange itself (two exchanges at once): both passed the SDK's lookup,
# the second finds the token used. The grant must end, and stay ended.


def provider_of(app):
    return app.state.oauth_provider


def grant_revoked(app) -> bool:
    return rows(app, "SELECT revoked_at FROM oauth_grants")[0].revoked_at is not None


@pytest.mark.anyio
async def test_a_code_exchanged_twice_at_once_ends_the_grant(app, agent, browser):
    client_id, verifier, code = code_for(agent, browser, posts="read")
    provider = provider_of(app)
    client = await provider.get_client(client_id)
    loaded = await provider.load_authorization_code(client, code)
    tokens = await provider.exchange_authorization_code(client, loaded)

    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, loaded)

    assert grant_revoked(app)
    assert api(agent, tokens.access_token).status_code == 401


@pytest.mark.anyio
async def test_a_refresh_replayed_during_its_exchange_ends_the_grant(app, agent, browser, clock):
    client_id, first = connect(agent, browser, posts="read")
    provider = provider_of(app)
    client = await provider.get_client(client_id)
    loaded = await provider.load_refresh_token(client, first["refresh_token"])
    await provider.exchange_refresh_token(client, loaded, [])
    clock.advance(seconds=31)

    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, loaded, [])

    assert grant_revoked(app)


def test_rows_past_their_lifetime_are_purged_on_refresh(app, agent, browser, clock):
    client_id, tokens = connect(agent, browser, posts="read")
    for _ in range(3):
        clock.advance(minutes=50)
        tokens = refresh(agent, client_id, tokens["refresh_token"]).json()
    clock.advance(days=29)
    sign_in(browser, clock)
    other_id, other = connect(agent, browser, posts="read")  # the first grant's still live then
    clock.advance(days=1, minutes=1)

    assert refresh(agent, other_id, other["refresh_token"]).status_code == 200

    # Only the new grant's tokens are left: its used refresh token and the new pair.
    assert len(rows(app, "SELECT hash FROM oauth_tokens")) == 3


def test_no_grace_once_the_new_pair_was_used(agent, browser, clock):
    client_id, first = connect(agent, browser, posts="read")
    second = refresh(agent, client_id, first["refresh_token"]).json()
    third = refresh(agent, client_id, second["refresh_token"]).json()

    replay = refresh(agent, client_id, first["refresh_token"])

    assert replay.status_code == 400
    assert api(agent, third["access_token"]).status_code == 401


def test_a_refresh_token_works_only_for_its_client(agent, browser):
    _, tokens = connect(agent, browser)
    other = register(agent).json()["client_id"]

    assert refresh(agent, other, tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_the_token_endpoint_is_limited_per_address(agent):
    statuses = [refresh(agent, "unknown", "jxr_made-up").status_code for _ in range(31)]

    assert statuses[-1] == 429 and 429 not in statuses[:30]


# Revocation


def revoke(agent, client_id, token, **extra):
    return agent.post("/revoke", data={"client_id": client_id, "token": token, **extra})


@pytest.mark.parametrize("which", ["access_token", "refresh_token"])
def test_revoking_either_token_ends_the_grant(app, agent, browser, which):
    client_id, tokens = connect(agent, browser, posts="read")

    response = revoke(agent, client_id, tokens[which])

    assert response.status_code == 200
    assert api(agent, tokens["access_token"]).status_code == 401
    assert refresh(agent, client_id, tokens["refresh_token"]).status_code == 400
    assert ("oauth.revoked", "ok") in audit_actions(app)


def test_revoking_an_old_refresh_token_is_not_a_replay(app, agent, browser, clock):
    client_id, first = connect(agent, browser, posts="read")
    second = refresh(agent, client_id, first["refresh_token"]).json()
    clock.advance(minutes=5)

    assert revoke(agent, client_id, first["refresh_token"]).status_code == 200

    assert api(agent, second["access_token"]).status_code == 200
    assert [
        a for a, _ in audit_actions(app) if a in {"oauth.revoked", "oauth.refresh_reused"}
    ] == []


def test_revocation_needs_the_tokens_own_client(agent, browser):
    client_id, tokens = connect(agent, browser, posts="read")
    other = register(agent).json()["client_id"]

    assert revoke(agent, other, tokens["access_token"]).status_code == 200
    assert api(agent, tokens["access_token"]).status_code == 200
    assert revoke(agent, "unknown", tokens["access_token"]).status_code == 401
    assert agent.post("/revoke", data={"token": tokens["access_token"]}).status_code == 400
    assert revoke(agent, client_id, "jxo_made-up").status_code == 200


@pytest.mark.parametrize("change", ["password", "totp"])
def test_changing_the_credentials_ends_every_grant(app, settings, agent, browser, clock, change):
    _, tokens = connect(agent, browser, posts="read")
    with app.state.sessionmaker() as session, session.begin():
        if change == "password":
            operator.set_password(session, app.state.network, "another long password", clock.now())
        else:
            key = crypto.load_key(settings.secret_key_file)
            code = pyotp.TOTP(TOTP_SECRET).at(clock.now() + timedelta(minutes=5))
            operator.enroll_totp(
                session,
                app.state.network,
                key,
                TOTP_SECRET,
                code,
                clock.now() + timedelta(minutes=5),
            )

    assert api(agent, tokens["access_token"]).status_code == 401
    assert rows(app, "SELECT revoked_at FROM oauth_grants")[0].revoked_at is not None


# The command line


def test_grants_are_listed_and_revoked_from_the_cli(
    app, settings, agent, browser, monkeypatch, capsys
):
    client_id, tokens = connect(agent, browser, posts="read")
    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(settings.database_path))

    main(["list-grants"])
    listing = capsys.readouterr().out
    grant = json.loads(listing)[0]
    main(["revoke-grant", "--id", grant["id"]])
    capsys.readouterr()

    assert grant["client_name"] == "Test agent"
    assert grant["permissions"] == {**ALL_DENIED, "posts": "read"}
    assert tokens["access_token"] not in listing and tokens["refresh_token"] not in listing
    assert api(agent, tokens["access_token"]).status_code == 401
    # The client stays registered and can ask again with the same id.
    _, verifier, code = code_for(agent, browser, client_id=client_id, posts="read")
    assert exchange(agent, client_id, code, verifier).status_code == 200


def test_the_cli_can_revoke_every_grant(settings, agent, browser, monkeypatch, capsys):
    _, first = connect(agent, browser, posts="read")
    _, second = connect(agent, browser, stats="read")
    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(settings.database_path))

    main(["revoke-grant", "--all"])

    assert json.loads(capsys.readouterr().out) == {"revoked": 2}
    assert api(agent, first["access_token"]).status_code == 401
    assert api(agent, second["access_token"]).status_code == 401


# The grant's absolute lifetime


def age_grant(app, age: timedelta, now: datetime) -> None:
    with app.state.sessionmaker() as session, session.begin():
        session.scalar(select(OAuthGrant)).created_at = now - age


def test_a_grant_past_its_lifetime_is_gone_until_the_operator_consents_again(
    app, agent, browser, clock
):
    client_id, tokens = connect(agent, browser, posts="read")
    assert api(agent, tokens["access_token"]).status_code == 200

    # Tokens still within their own lifetimes, as a grant older than 90 days has on upgrade.
    age_grant(app, store.GRANT_LIFETIME, clock.now())

    assert api(agent, tokens["access_token"]).status_code == 401
    assert refresh(agent, client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"
    assert not grant_revoked(app)  # it ran out; nobody ended it

    _, verifier, code = code_for(agent, browser, client_id=client_id, posts="read")
    renewed = exchange(agent, client_id, code, verifier).json()
    assert api(agent, renewed["access_token"]).status_code == 200


def test_tokens_issued_near_the_end_of_a_grant_do_not_outlive_it(app, agent, browser, clock):
    client_id, tokens = connect(agent, browser, posts="read")
    age_grant(app, store.GRANT_LIFETIME - timedelta(minutes=30), clock.now())

    renewed = refresh(agent, client_id, tokens["refresh_token"]).json()

    assert renewed["expires_in"] == 1800
    with app.state.read_sessionmaker() as session:
        live = session.scalars(select(OAuthToken).where(OAuthToken.used_at.is_(None))).all()
    assert {token.expires_at for token in live} == {clock.now() + timedelta(minutes=30)}
    assert api(agent, renewed["access_token"]).status_code == 200

    clock.advance(minutes=30)

    assert api(agent, renewed["access_token"]).status_code == 401
    assert refresh(agent, client_id, renewed["refresh_token"]).json()["error"] == "invalid_grant"
