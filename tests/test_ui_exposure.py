"""What exposing /ui behind a reverse proxy relies on: sign-in limits per client address, bounded
argon2 cost, the CSRF origin check, the return path after signing in, and the API answering 404
to whatever comes through the proxy. The real proxy headers are in test_proxy_headers.py."""

import logging
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from jorgex_social.access import operator
from jorgex_social.access.limits import RateLimiter, client_key
from jorgex_social.account import appearance, crypto
from jorgex_social.api import create_app
from jorgex_social.clock import ManualClock
from jorgex_social.ui.routes import BUSY, COOKIE, INVALID, TOO_MANY, safe_next

START = datetime(2026, 10, 30, 12, 0, 10, tzinfo=UTC)
PASSWORD = "correct horse battery staple"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
LOCAL = "http://localhost"
PUBLIC = "https://social.example.com"
VIA_PROXY = {"x-forwarded-for": "203.0.113.7"}


@pytest.fixture
def clock():
    return ManualClock(START)


@pytest.fixture
def app(settings, clock):
    return create_app(replace(settings, public_url=PUBLIC), clock)


@pytest.fixture
def enrolled(app, settings, clock):
    key = crypto.load_key(settings.secret_key_file)
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


def browser(app, address="198.51.100.1", origin=LOCAL) -> TestClient:
    return TestClient(app, base_url=LOCAL, headers={"origin": origin}, client=(address, 50000))


def attempt(client, clock, password="wrong password!!", **extra):
    data = {"password": password, "code": pyotp.TOTP(TOTP_SECRET).at(clock.now()), **extra}
    return client.post("/ui/login", data=data, follow_redirects=False)


def audit_rows(app) -> int:
    with app.state.read_sessionmaker() as session, session.begin():
        return session.scalar(text("SELECT count(*) FROM audit_log"))


# Sign-in limits


@pytest.mark.usefixtures("enrolled")
def test_five_attempts_a_minute_per_address(app, clock, caplog):
    client = browser(app)
    for _ in range(5):
        assert attempt(client, clock).status_code == 401
    audited = audit_rows(app)

    refused = attempt(client, clock, password=PASSWORD)

    assert refused.status_code == 429
    assert TOO_MANY in refused.text
    assert int(refused.headers["retry-after"]) == 61
    assert audit_rows(app) == audited  # logged only
    assert "too many attempts" in caplog.text
    clock.advance(minutes=1)
    assert attempt(client, clock, password=PASSWORD).status_code == 303


@pytest.mark.usefixtures("enrolled")
def test_twenty_attempts_an_hour_per_address(app, clock):
    client = browser(app)
    for _ in range(20):
        assert attempt(client, clock).status_code == 401
        clock.advance(minutes=2)

    assert attempt(client, clock, password=PASSWORD).status_code == 429
    clock.advance(minutes=20)  # the first attempts are now an hour old
    assert attempt(client, clock, password=PASSWORD).status_code == 303


@pytest.mark.usefixtures("enrolled")
def test_one_address_over_the_limit_does_not_keep_another_out(app, clock):
    attacker = browser(app, "198.51.100.66")
    for _ in range(10):
        attempt(attacker, clock)

    assert attempt(browser(app, "192.0.2.10"), clock, password=PASSWORD).status_code == 303


def test_refused_attempts_never_pay_for_argon2_or_write(app, clock, monkeypatch, enrolled):
    client = browser(app)
    for _ in range(5):
        attempt(client, clock)

    def forbidden(*_):
        raise AssertionError("an attempt over the limit reached the password check")

    monkeypatch.setattr(operator, "_password_matches", forbidden)
    monkeypatch.setattr(app.state, "sessionmaker", forbidden)
    monkeypatch.setattr(app.state, "read_sessionmaker", forbidden)

    assert attempt(client, clock).status_code == 429


@pytest.mark.parametrize(
    ("first", "second", "shared"),
    [
        ("2001:db8:1:2::1", "2001:db8:1:2:ffff:ffff:ffff:ffff", True),  # same /64
        ("2001:db8:1:2::1", "2001:db8:1:3::1", False),
        ("::ffff:198.51.100.1", "198.51.100.1", True),  # IPv4 mapped into IPv6
        ("198.51.100.1", "198.51.100.2", False),
    ],
)
def test_ipv6_clients_are_counted_per_64(first, second, shared):
    assert (client_key(first) == client_key(second)) is shared


@pytest.mark.usefixtures("enrolled")
def test_ipv6_addresses_of_one_64_share_the_limit(app, clock):
    for host in range(5):
        attempt(browser(app, f"2001:db8:1:2::{host + 1}"), clock)

    assert attempt(browser(app, "2001:db8:1:2::99"), clock).status_code == 429
    assert attempt(browser(app, "2001:db8:1:3::1"), clock).status_code == 401


def test_the_limiter_forgets_the_least_recent_client_past_its_cap():
    limiter = RateLimiter([(1, timedelta(minutes=1))], max_keys=3)
    for key in ("a", "b", "c", "d"):
        assert limiter.hit(key, START) is None

    assert len(limiter) == 3
    assert limiter.hit("c", START) is not None  # still remembered, still over its limit
    assert limiter.hit("a", START) is None  # forgotten: "a" was the least recent


def test_the_limiter_drops_idle_clients():
    limiter = RateLimiter([(5, timedelta(minutes=1)), (20, timedelta(hours=1))])
    for n in range(100):
        limiter.hit(f"198.51.100.{n}", START)

    limiter.hit("192.0.2.1", START + timedelta(hours=1, seconds=1))

    assert len(limiter) == 1


@pytest.mark.parametrize(
    ("address", "headers", "warned"),
    [
        ("198.51.100.250", {}, True),  # the trusted proxy sent no X-Forwarded-For
        ("198.51.100.9", {"x-forwarded-for": "203.0.113.7"}, True),  # a header not believed
        ("203.0.113.7", {"x-forwarded-for": "203.0.113.7"}, False),  # believed: a visitor
        ("198.51.100.9", {}, False),  # a visitor straight to the port
    ],
)
def test_a_limit_on_the_proxys_own_address_is_flagged(
    settings, clock, caplog, address, headers, warned
):
    exposed = replace(settings, public_url=PUBLIC, forwarded_allow_ips="198.51.100.250")
    client = TestClient(create_app(exposed, clock), base_url=LOCAL, client=(address, 50000))
    headers = {"origin": PUBLIC, **headers}

    for _ in range(6):
        client.post("/ui/login", data={"password": "x", "code": "123456"}, headers=headers)

    assert ("every visitor shares one count" in caplog.text) is warned


def test_limits_are_configurable(settings, clock):
    app = create_app(replace(settings, login_limit_per_minute=2), clock)
    client = browser(app)

    assert [attempt(client, clock).status_code for _ in range(3)] == [401, 401, 429]


# Bounded argon2


@pytest.mark.usefixtures("enrolled")
def test_a_sign_in_waits_briefly_for_a_password_check_then_gives_up(app, clock, monkeypatch):
    monkeypatch.setattr(operator, "PASSWORD_CHECK_WAIT", 0.05)
    assert all(operator._password_checks.acquire(timeout=1) for _ in range(2))

    def free_both():
        operator._password_checks.release()
        operator._password_checks.release()

    # Freed later in any case, so a wait without bound would end (and fail) instead of hanging.
    timer = threading.Timer(1.0, free_both)
    timer.start()
    try:
        response = attempt(browser(app), clock, password=PASSWORD)
    finally:
        timer.join()

    assert response.status_code == 503
    assert BUSY in response.text
    assert COOKIE not in response.headers.get("set-cookie", "")


@pytest.mark.usefixtures("enrolled")
def test_past_eight_sign_ins_in_flight_the_next_is_refused_at_once(app, clock, monkeypatch):
    """Waiting sign-ins hold server threads; past the bound they are not allowed to wait."""
    monkeypatch.setattr(operator, "PASSWORD_CHECK_WAIT", 5.0)
    assert all(operator._password_attempts.acquire(timeout=1) for _ in range(8))
    try:
        started = time.monotonic()
        response = attempt(browser(app), clock, password=PASSWORD)
        elapsed = time.monotonic() - started
    finally:
        for _ in range(8):
            operator._password_attempts.release()

    assert response.status_code == 503
    assert BUSY in response.text
    assert elapsed < 1.0


def test_a_free_slot_is_waited_for(monkeypatch):
    # The wait is bounded, not zero: a check that frees its slot in time lets the next one run.
    monkeypatch.setattr(operator, "PASSWORD_CHECK_WAIT", 2.0)
    taken = [operator._password_checks.acquire(timeout=1) for _ in range(2)]
    timer = threading.Timer(0.05, operator._password_checks.release)
    timer.start()
    try:
        assert operator._password_matches("not an argon2 hash", "pw") is False
    finally:
        timer.join()
        operator._password_checks.release()
    assert all(taken)


# CSRF


def _post_login(app, headers, client=("198.51.100.1", 50000), base_url=LOCAL):
    return TestClient(app, base_url=base_url, client=client).post(
        "/ui/login",
        data={"password": "x", "code": "123456"},
        headers=headers,
        follow_redirects=False,
    )


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({"origin": PUBLIC, **VIA_PROXY}, True),
        ({"origin": "https://social.example.com:443", **VIA_PROXY}, True),
        ({"origin": "https://evil.example.net", **VIA_PROXY}, False),
        ({"origin": "http://social.example.com", **VIA_PROXY}, False),  # scheme differs
        ({"origin": "https://social.example.com.evil.example.net", **VIA_PROXY}, False),
        ({"origin": "null", **VIA_PROXY}, False),
        ({"origin": "http://localhost:8000", **VIA_PROXY}, False),  # loopback only off the proxy
        ({"origin": "http://localhost:8000"}, True),  # the SSH tunnel
        ({"origin": "http://127.0.0.1:9000"}, True),
        ({"origin": "http://[::1]:8000"}, True),
        ({"origin": "http://localhost.evil.example.net"}, False),
        ({}, False),  # neither Origin nor Sec-Fetch-Site: not a browser form
        ({"sec-fetch-site": "same-origin"}, True),
        # What a browser sends under our Referrer-Policy: no-referrer.
        ({"origin": "null", "sec-fetch-site": "same-origin", **VIA_PROXY}, True),
        ({"origin": "null", "sec-fetch-site": "cross-site", **VIA_PROXY}, False),
        ({"sec-fetch-site": "cross-site"}, False),
        ({"sec-fetch-site": "same-site"}, False),
    ],
)
def test_login_posts_must_come_from_the_public_url_or_the_tunnel(app, headers, allowed):
    response = _post_login(app, headers)

    assert (response.status_code != 403) is allowed, response.status_code


def test_without_a_public_url_only_loopback_origins_post(settings, clock):
    app = create_app(settings, clock)

    assert _post_login(app, {"origin": "http://localhost:8000"}).status_code == 401
    assert _post_login(app, {"origin": PUBLIC}).status_code == 403
    assert _post_login(app, {"origin": PUBLIC, **VIA_PROXY}).status_code == 403


def test_a_cross_site_post_is_refused_before_counting_an_attempt(app, clock):
    for _ in range(10):
        assert _post_login(app, {"origin": "https://evil.example.net"}).status_code == 403

    assert _post_login(app, {"origin": LOCAL}).status_code == 401


@pytest.mark.usefixtures("enrolled")
def test_logout_needs_the_origin_too(app, clock):
    client = browser(app)
    attempt(client, clock, password=PASSWORD)

    forged = client.post("/ui/logout", headers={"origin": "https://evil.example.net"})

    assert forged.status_code == 403
    with app.state.read_sessionmaker() as session, session.begin():
        assert session.scalar(text("SELECT count(*) FROM sessions")) == 1


# Returning after signing in


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/ui/posts", "/ui/posts"),
        ("/ui/stats/123?days=7", "/ui/stats/123?days=7"),
        ("/ui", "/ui"),
        (None, "/ui/"),
        ("", "/ui/"),
        ("//evil.example.net/ui/", "/ui/"),
        ("https://evil.example.net/ui/", "/ui/"),
        ("/\\evil.example.net", "/ui/"),
        ("/ui\\..\\v1", "/ui/"),
        ("/\t/evil.example.net", "/ui/"),
        ("/ui/../v1/token", "/ui/"),
        ("/ui/%2e%2e/v1/token", "/ui/"),
        ("/uix", "/ui/"),
        ("/v1/token", "/ui/"),
        ("javascript:alert(1)", "/ui/"),
        ("ui/posts", "/ui/"),
    ],
)
def test_next_is_only_a_path_of_the_interface(value, expected):
    assert safe_next(value) == expected


@pytest.mark.usefixtures("enrolled")
def test_signing_in_returns_to_the_page_asked_for(app, clock):
    client = browser(app)

    to_login = client.get("/ui/posts?status=failed", follow_redirects=False)
    assert to_login.headers["location"] == "/ui/login?next=%2Fui%2Fposts%3Fstatus%3Dfailed"
    form = client.get(to_login.headers["location"])
    assert 'name="next" value="/ui/posts?status=failed"' in form.text

    response = attempt(client, clock, password=PASSWORD, next="/ui/posts?status=failed")

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/posts?status=failed"


@pytest.mark.usefixtures("enrolled")
def test_an_unsafe_next_goes_home(app, clock):
    response = attempt(browser(app), clock, password=PASSWORD, next="https://evil.example.net/")

    assert response.headers["location"] == "/ui/"


def test_a_failed_sign_in_keeps_the_page_asked_for(app, clock):
    response = attempt(browser(app), clock, next="/ui/rules")

    assert INVALID in response.text
    assert 'name="next" value="/ui/rules"' in response.text


# The API stays behind the tunnel


TUNNEL_ONLY = ["/v1/token", "/v1/posts", "/v1", "/docs", "/docs/oauth2-redirect", "/redoc"]


@pytest.mark.parametrize("path", [*TUNNEL_ONLY, "/openapi.json"])
@pytest.mark.parametrize("header", ["x-forwarded-for", "forwarded", "x-real-ip"])
def test_the_api_and_its_docs_are_not_found_through_the_proxy(app, path, header):
    client = TestClient(app)  # simulation: the docs are served off the proxy

    assert client.get(path, headers={header: "203.0.113.7"}).status_code == 404
    assert client.post(path, headers={header: "203.0.113.7"}).status_code == 404


def test_the_api_still_answers_through_the_tunnel(app):
    client = TestClient(app)

    assert client.get("/v1/token").status_code == 401
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200


def test_what_the_proxy_publishes_still_answers(app):
    client = TestClient(app, headers=VIA_PROXY)

    assert client.get("/health").status_code == 200
    assert client.get("/ui/login").status_code == 200
    assert client.get("/ui/static/style.css").status_code == 200
    assert client.get("/ui/icon").headers["content-type"].startswith("image/svg+xml")
    assert client.get("/ui/appearance.css").status_code == 200
    assert client.get("/v1x").status_code == 404  # an unknown path, as before


def test_through_the_proxy_nothing_is_audited_for_the_api(app, caplog):
    with caplog.at_level(logging.INFO):
        TestClient(app).post("/v1/posts", headers=VIA_PROXY)

    assert "unauthenticated" not in caplog.text
    assert audit_rows(app) == 0


def test_the_appearance_api_is_not_public_but_what_pages_need_is(app):
    client = TestClient(app, headers=VIA_PROXY)

    assert client.get("/v1/appearance").status_code == 404
    assert client.put("/v1/appearance", json={"name": "Hacked"}).status_code == 404
    assert client.get("/ui/settings", follow_redirects=False).status_code == 303
    assert client.get("/ui/icon").status_code == 200


@pytest.mark.usefixtures("enrolled")
def test_the_consent_card_shows_the_name_and_icon_without_the_menu(app, clock):
    client = browser(app)
    assert attempt(client, clock, password=PASSWORD).status_code == 303
    with app.state.sessionmaker() as session, session.begin():
        appearance.update(session, clock.now(), name="Acme One", name_highlight="One")

    page = client.get("/ui/oauth/consent?req=gone").text

    assert 'Acme <span class="accent">One</span>' in page
    assert '<img class="wordmark-icon" src="/ui/icon"' in page
    assert "profile-menu" not in page
