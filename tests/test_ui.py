import asyncio
import hashlib
import io
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from jorgex_social.access import operator
from jorgex_social.access.models import Operator, OperatorSession
from jorgex_social.access.tokens import create_token
from jorgex_social.account import appearance, crypto, scope
from jorgex_social.account.credential import seal
from jorgex_social.account.models import ChannelIncident, Connection
from jorgex_social.api import create_app
from jorgex_social.cli import main
from jorgex_social.clock import ManualClock
from jorgex_social.publishing.models import Media, Post, PostItem, PublishAttempt
from jorgex_social.replies.models import Delivery, Event, Rule
from jorgex_social.stats.models import AccountSnapshot, PostSnapshot
from jorgex_social.ui.routes import ACCOUNT_COOKIE, COOKIE, INVALID, cookie_is_secure

START = datetime(2026, 10, 30, 12, 0, 10, tzinfo=UTC)
PASSWORD = "correct horse battery staple"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
# The pages of one account, as the suffix of `/ui/a/{account}`: "" is its home.
SECTIONS = ["", "/posts", "/stats", "/rules", "/events", "/incidents"]
# The URLs from before the account went into the path: a bookmark may still hold one.
LEGACY = ["/ui/posts", "/ui/stats", "/ui/rules", "/ui/events", "/ui/incidents"]


@pytest.fixture
def clock():
    return ManualClock(START)


@pytest.fixture
def app(settings, clock):
    return create_app(settings, clock)


LOCAL = "http://localhost"


@pytest.fixture
def client(app):
    # Plain http to localhost, like the SSH tunnel: the cookie is not Secure, so the test client
    # sends it back. Its form posts carry the Origin a browser would send.
    return TestClient(app, base_url=LOCAL, headers={"origin": LOCAL})


@pytest.fixture
def enrolled(app, settings, clock):
    """An operator with the second factor enrolled one step before START."""
    key = crypto.load_key(settings.secret_key_file)
    earlier = clock.now() - timedelta(seconds=30)
    with app.state.sessionmaker() as session, session.begin():
        operator.create_operator(session, PASSWORD, earlier)
        operator.enroll_totp(
            session,
            key,
            TOTP_SECRET,
            pyotp.TOTP(TOTP_SECRET).at(earlier),
            earlier,
        )


def code_at(when: datetime) -> str:
    return pyotp.TOTP(TOTP_SECRET).at(when)


def wrong_code(now: datetime) -> str:
    valid = {code_at(now + timedelta(seconds=s)) for s in (-30, 0, 30)}
    return next(c for c in ("000000", "111111", "222222") if c not in valid)


def log_in(client, clock, password=PASSWORD, code=None):
    return client.post(
        "/ui/login",
        data={"password": password, "code": code or code_at(clock.now())},
        follow_redirects=False,
    )


def rows(app, query, **params):
    with app.state.read_sessionmaker() as session, session.begin():
        return session.execute(text(query), params).all()


# Without a session


@pytest.mark.parametrize(
    "path",
    [
        "/ui/",
        "/ui/settings",
        "/ui/settings/appearance",
        *LEGACY,
        "/ui/posts/any",
        "/ui/stats/any",
        "/ui/media/any",
    ],
)
def test_pages_redirect_to_login_without_a_session(client, path):
    response = client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].split("?")[0] == "/ui/login"


def test_unknown_session_cookie_redirects_and_is_cleared(client):
    response = client.get("/ui/", headers={"cookie": f"{COOKIE}=made-up"}, follow_redirects=False)

    assert response.headers["location"] == "/ui/login"
    assert f'{COOKIE}=""' in response.headers["set-cookie"]


# Signing in


@pytest.mark.usefixtures("enrolled")
def test_sign_in_opens_the_pages(client, clock):
    response = log_in(client, clock)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/"
    assert client.get("/ui/", follow_redirects=False).status_code == 200


@pytest.mark.usefixtures("enrolled")
def test_wrong_password_and_wrong_code_get_the_same_answer(client, clock):
    wrong_password = log_in(client, clock, password="not the password at all")
    bad_code = log_in(client, clock, code=wrong_code(clock.now()))

    assert wrong_password.status_code == bad_code.status_code == 401
    assert INVALID in wrong_password.text
    assert wrong_password.text == bad_code.text
    assert COOKIE not in client.cookies


def test_no_operator_gets_the_same_answer(client, clock):
    response = log_in(client, clock)

    assert response.status_code == 401
    assert INVALID in response.text
    assert rows(app=client.app, query="SELECT * FROM audit_log") == []  # nothing to count


def test_operator_without_totp_cannot_sign_in(app, client, clock):
    with app.state.sessionmaker() as session, session.begin():
        operator.create_operator(session, PASSWORD, clock.now())

    response = log_in(client, clock)

    assert response.status_code == 401
    assert INVALID in response.text


@pytest.mark.usefixtures("enrolled")
def test_failures_never_lock_the_operator_out(app, client, clock):
    """Anyone can fail on purpose; a lockout of the one account would keep the operator out.
    Guessing is bounded per address instead (test_ui_exposure.py)."""
    for _ in range(8):
        assert log_in(client, clock, password="wrong password!!").status_code == 401
        clock.advance(minutes=1)

    assert log_in(client, clock).status_code == 303

    actions = rows(
        app,
        "SELECT action, outcome, actor_type FROM audit_log ORDER BY occurred_at, id",
    )
    assert [tuple(a) for a in actions] == [
        ("operator.created", "ok", "operator"),
        ("operator.totp_enrolled", "ok", "operator"),
        *[("operator.login", "denied", "operator")] * 8,
        ("operator.login", "ok", "operator"),
    ]


@pytest.mark.usefixtures("enrolled")
def test_a_totp_code_is_accepted_once(client, clock):
    code = code_at(clock.now())
    assert log_in(client, clock, code=code).status_code == 303
    client.post("/ui/logout")

    replay = log_in(client, clock, code=code)

    assert replay.status_code == 401
    assert INVALID in replay.text


@pytest.mark.usefixtures("enrolled")
def test_an_older_code_is_refused_after_a_newer_one(client, clock):
    older = code_at(clock.now() - timedelta(seconds=30))  # within the drift allowance
    assert log_in(client, clock, code=code_at(clock.now())).status_code == 303

    assert log_in(client, clock, code=older).status_code == 401


@pytest.mark.usefixtures("enrolled")
def test_the_enrolment_code_cannot_sign_in(client, clock):
    enrolment = code_at(clock.now() - timedelta(seconds=30))

    assert log_in(client, clock, code=enrolment).status_code == 401


# Sessions


@pytest.mark.usefixtures("enrolled")
def test_session_cookie_flags_over_https(settings, clock):
    public = "https://social.example.com"
    app = create_app(replace(settings, public_url=public), clock)
    client = TestClient(app, base_url=public, headers={"origin": public})

    response = log_in(client, clock)

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/ui" in cookie


def _request(app, url: str) -> Request:
    scheme, rest = url.split("://")
    host = rest.split("/")[0]
    return Request(
        {
            "type": "http",
            "app": app,
            "scheme": scheme,
            "server": (host.split(":")[0], 80),
            "path": "/ui/login",
            "headers": [(b"host", host.encode())],
            "query_string": b"",
        }
    )


LOCAL_AND_REMOTE = [
    ("http://localhost:8000", False),
    ("http://127.0.0.1:8000", False),
    ("http://127.8.9.10:8000", False),  # all of 127.0.0.0/8
    ("http://[::1]:8000", False),
    ("https://localhost:8000", True),
    ("http://social.example.com", True),
    ("https://social.example.com", True),
    ("http://10.0.0.5:8000", True),
    ("http://128.0.0.1", True),
    ("http://localhost.example.com", True),
]


@pytest.mark.parametrize("simulation", [True, False])
@pytest.mark.parametrize(("url", "secure"), LOCAL_AND_REMOTE)
def test_cookie_is_secure_except_over_plain_http_to_loopback(settings, simulation, url, secure):
    """The SSH tunnel reaches the interface at http://localhost, in real mode too."""
    app = create_app(replace(settings, simulation=simulation))

    assert cookie_is_secure(_request(app, url)) is secure


@pytest.mark.usefixtures("enrolled")
def test_only_the_hash_of_the_session_id_is_stored(app, client, clock):
    log_in(client, clock)
    session_id = client.cookies[COOKIE]

    stored = rows(app, "SELECT id_hash FROM sessions")

    assert stored == [(hashlib.sha256(session_id.encode()).hexdigest(),)]
    dump = "\n".join(_dump(app))
    assert session_id not in dump


@pytest.mark.usefixtures("enrolled")
def test_idle_session_expires_after_a_day(client, clock):
    log_in(client, clock)

    clock.advance(hours=23)
    assert client.get("/ui/", follow_redirects=False).status_code == 200  # activity recorded
    clock.advance(hours=23)
    assert client.get("/ui/", follow_redirects=False).status_code == 200
    clock.advance(hours=24)
    response = client.get("/ui/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


@pytest.mark.usefixtures("enrolled")
def test_session_expires_after_seven_days_even_when_active(client, clock):
    log_in(client, clock)
    for _ in range(7):  # 161 h of activity
        clock.advance(hours=23)
        assert client.get("/ui/", follow_redirects=False).status_code == 200

    clock.advance(hours=6, minutes=59)
    assert client.get("/ui/", follow_redirects=False).status_code == 200
    clock.advance(minutes=1)

    assert client.get("/ui/", follow_redirects=False).status_code == 303


@pytest.mark.usefixtures("enrolled")
def test_signing_in_again_ends_the_session_it_replaces(app, client, clock):
    log_in(client, clock)
    replaced = client.cookies[COOKIE]
    clock.advance(seconds=30)  # each sign-in needs a fresh TOTP step

    assert log_in(client, clock).status_code == 303

    assert client.cookies[COOKIE] != replaced
    assert client.get("/ui/", follow_redirects=False).status_code == 200
    thief = TestClient(app, base_url=LOCAL, cookies={COOKIE: replaced})
    assert thief.get("/ui/", follow_redirects=False).headers["location"] == "/ui/login"


@pytest.mark.usefixtures("enrolled")
def test_a_failed_sign_in_leaves_the_session_alone(client, clock):
    log_in(client, clock)
    clock.advance(seconds=30)

    assert log_in(client, clock, password="wrong").status_code == 401

    assert client.get("/ui/", follow_redirects=False).status_code == 200


@pytest.mark.usefixtures("enrolled")
def test_signing_in_with_a_made_up_cookie_works(client, clock):
    response = client.post(
        "/ui/login",
        data={"password": PASSWORD, "code": code_at(clock.now())},
        headers={"cookie": f"{COOKIE}=made-up"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert client.get("/ui/", follow_redirects=False).status_code == 200


@pytest.mark.usefixtures("enrolled")
def test_logout_deletes_the_session(app, client, clock):
    log_in(client, clock)
    session_id = client.cookies[COOKIE]

    response = client.post("/ui/logout", follow_redirects=False)

    assert response.headers["location"] == "/ui/login"
    assert rows(app, "SELECT count(*) FROM sessions") == [(0,)]
    stale = client.get("/ui/", headers={"cookie": f"{COOKIE}={session_id}"}, follow_redirects=False)
    assert stale.status_code == 303


@pytest.mark.usefixtures("enrolled")
def test_changing_the_password_signs_every_browser_out(app, client, clock):
    log_in(client, clock)

    with app.state.sessionmaker() as session, session.begin():
        operator.set_password(session, "another long password", clock.now())

    assert client.get("/ui/", follow_redirects=False).status_code == 303


def test_the_database_holds_one_operator_at_most(app, clock):
    with app.state.sessionmaker() as session, session.begin():
        operator.create_operator(session, PASSWORD, clock.now())
    with (
        pytest.raises(operator.OperatorError),
        app.state.sessionmaker() as session,
        session.begin(),
    ):
        operator.create_operator(session, PASSWORD, clock.now())
    # Also refused by the database itself.
    with pytest.raises(IntegrityError), app.state.sessionmaker() as session, session.begin():
        session.add(Operator(password_hash="x"))


# Pages


@pytest.mark.usefixtures("enrolled")
@pytest.mark.parametrize("path", SECTIONS)
def test_account_pages_render_with_an_empty_database(client, clock, connected, path):
    log_in(client, clock)

    response = client.get(f"{connected}{path or '/'}")

    assert response.status_code == 200
    assert "<script" not in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.usefixtures("enrolled")
@pytest.mark.parametrize("path", ["/ui/", "/ui/settings/appearance"])
def test_the_installation_pages_render_with_an_empty_database(client, clock, path):
    log_in(client, clock)

    response = client.get(path)

    assert response.status_code == 200
    assert "<script" not in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


API_TOKEN_NAME = "agent"
CREDENTIAL = "instagram-credential-not-real"


@pytest.fixture
def connected(app):
    """The account of the mode, connected: `/ui/a/{account}/…` serves connected accounts only. It
    returns the base of its pages, which is where every link of them starts."""
    with app.state.sessionmaker() as session, session.begin():
        account = scope.current_account(session, app.state.network)
        account.external_account_id = "17840000000000001"
        account.username = "sample_account"
        return f"/ui/a/{account.id}"


@pytest.fixture
def sample(app, settings, clock, enrolled):
    """One of everything the pages show, plus every secret they must never show."""
    key = crypto.load_key(settings.secret_key_file)
    now = clock.now()
    network = app.state.network
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    (settings.media_dir / "photo.jpeg").write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)
    with app.state.sessionmaker() as session, session.begin():
        connection = scope.current_account(session, network)
        _, api_secret = create_token(session, API_TOKEN_NAME, {"posts": "read"}, connection.id)
        connection.external_account_id = "17840000000000001"
        connection.username = "sample_account"
        connection.account_type = "BUSINESS"
        connection.credential_encrypted = seal(key, connection.id, CREDENTIAL)
        connection.credential_expires_at = now + timedelta(days=40)
        connection.limits = {
            "quota_usage": 3,
            "quota_total": 100,
            "quota_duration_seconds": 86400,
        }
        connection.checked_at = now
        account_id = connection.id
        photo = Media(
            account_id=account_id,
            relative_path="photo.jpeg",
            sha256="a" * 64,
            type="jpeg",
            bytes=68,
        )
        session.add(photo)
        session.flush()
        published = Post(
            account_id=account_id,
            type="image",
            status="published",
            caption="A caption written by the operator",
            content_hash="b" * 64,
            published_at=now - timedelta(days=2),
            external_ref="18000000000000001",
            permalink="https://www.instagram.com/p/sample/",
            items=[PostItem(position=0, media_id=photo.id)],
        )
        scheduled = Post(
            account_id=account_id,
            type="image",
            status="scheduled",
            caption="Next one",
            content_hash="c" * 64,
            scheduled_for=now + timedelta(days=1),
            items=[PostItem(position=0, media_id=photo.id)],
        )
        session.add_all([published, scheduled])
        session.flush()
        session.add(
            PublishAttempt(
                post_id=published.id,
                number=1,
                started_at=now - timedelta(days=2),
                ended_at=now - timedelta(days=2),
                result="ok",
                container_ref="17900000000000001",
            )
        )
        for day in range(3):
            start = datetime(2026, 10, 26 + day, tzinfo=UTC)
            session.add(
                AccountSnapshot(
                    account_id=account_id,
                    period_start=start,
                    period_end=start + timedelta(days=1),
                    taken_at=now,
                    followers=1200 + day,
                    metrics={"reach": 300 + day * 40, "views": 500, "accounts_engaged": 20},
                )
            )
        for hours in (1, 25):
            session.add(
                PostSnapshot(
                    account_id=account_id,
                    media_ref="18000000000000001",
                    taken_at=now - timedelta(hours=hours),
                    product_type="FEED",
                    published_at=now - timedelta(days=2),
                    permalink="https://www.instagram.com/p/sample/",
                    metrics={"reach": 100 + hours, "likes": 7},
                )
            )
        rule = Rule(
            account_id=account_id,
            name="Price question",
            channel="comment",
            priority=10,
            active=True,
            activated_at=now,
            content={
                "keywords": ["price"],
                "exclude": [],
                "match": "word",
                "public_replies": ["Sent you a message"],
                "private_replies": ["Here is the price list"],
            },
            created_at=now,
            updated_at=now,
        )
        session.add(rule)
        session.flush()
        event = Event(
            account_id=account_id,
            type="comment",
            external_ref="17850000000000001",
            media_ref="18000000000000001",
            occurred_at=now,
            received_at=now,
            rule_id=rule.id,
        )
        session.add(event)
        session.flush()
        session.add(
            Delivery(
                account_id=account_id,
                event_id=event.id,
                rule_id=rule.id,
                action="private_reply",
                status="sent",
                text="Here is the price list",
                created_at=now,
            )
        )
        session.add(
            ChannelIncident(
                account_id=account_id,
                type="credential_expiring",
                opened_at=now - timedelta(hours=3),
                detail={"days_left": 9},
            )
        )
        post_id = published.id
        token_hash = session.scalar(text("SELECT hash FROM api_tokens"))
    password_hash = rows(app, "SELECT password_hash FROM operator")[0][0]
    return {
        "post_id": post_id,
        "media_id": photo.id,
        "base": f"/ui/a/{account_id}",
        "secrets": [
            api_secret,
            token_hash,
            CREDENTIAL,
            key.hex(),
            TOTP_SECRET,
            PASSWORD,
            password_hash,
        ],
    }


def _dump(app) -> list[str]:
    with app.state.read_sessionmaker() as session, session.begin():
        return list(session.connection().connection.driver_connection.iterdump())


def _sample_paths(sample) -> list[str]:
    base = sample["base"]
    return [
        f"{base}/",
        f"{base}/posts",
        f"{base}/stats",
        f"{base}/rules",
        f"{base}/events",
        f"{base}/incidents",
        "/ui/settings/appearance",
        f"{base}/posts/{sample['post_id']}",
        f"{base}/stats/18000000000000001",
        f"{base}/media/{sample['media_id']}",
    ]


def test_pages_render_sample_data_without_writing_or_showing_secrets(app, client, clock, sample):
    log_in(client, clock)
    base = sample["base"]
    before = _dump(app)

    bodies = {}
    for path in _sample_paths(sample):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 200, path
        bodies[path] = response.text

    assert _dump(app) == before
    session_id = client.cookies[COOKIE]
    for path, body in bodies.items():
        for secret in [*sample["secrets"], session_id]:
            assert secret not in body, f"{path} shows a secret"
    assert "@sample_account" in bodies[f"{base}/"]
    assert "Next one" in bodies[f"{base}/"]
    assert "A caption written by the operator" in bodies[f"{base}/posts"]
    assert f'src="{base}/media/{sample["media_id"]}"' in bodies[f"{base}/posts"]
    assert '<svg class="chart chart--primary"' in bodies[f"{base}/stats"]
    assert "Price question" in bodies[f"{base}/events"]
    assert "Here is the price list" in bodies[f"{base}/rules"]
    assert "not a published Meta rule" in bodies[f"{base}/rules"]  # one public variant: a warning
    assert "credential_expiring" in bodies[f"{base}/incidents"]


def test_the_rules_page_shows_a_rule_waiting_for_its_post(app, client, clock, enrolled, connected):
    with app.state.sessionmaker() as session, session.begin():
        account = scope.current_account(session, app.state.network)
        post = Post(account_id=account.id, type="image", content_hash="hash")
        session.add(post)
        session.flush()
        post_id = post.id
        session.add(
            Rule(
                account_id=account.id,
                name="Waiting",
                channel="comment",
                post_id=post_id,
                priority=1,
                content={"keywords": ["price"], "private_replies": ["Hi"]},
                created_at=clock.now(),
                updated_at=clock.now(),
            )
        )
    log_in(client, clock)

    page = client.get(f"{connected}/rules").text

    assert f"waiting for post {post_id}" in page
    assert "every post" not in page


def test_pages_only_write_the_session_activity(app, client, clock, sample):
    log_in(client, clock)
    clock.advance(minutes=5)
    before = [line for line in _dump(app) if 'INTO "sessions"' not in line]

    for path in _sample_paths(sample):
        assert client.get(path).status_code == 200

    assert [line for line in _dump(app) if 'INTO "sessions"' not in line] == before
    with app.state.read_sessionmaker() as session:
        (stored,) = session.scalars(select(OperatorSession)).all()
    assert stored.last_activity_at == clock.now()


@pytest.mark.usefixtures("enrolled")
def test_drafts_and_cancelled_posts_are_listed_last_and_folded(app, client, clock, sample):
    with app.state.sessionmaker() as session, session.begin():
        account = scope.current_account(session, app.state.network)
        for status, caption in (("draft", "A draft in progress"), ("cancelled", "Called off")):
            session.add(
                Post(
                    account_id=account.id,
                    type="image",
                    status=status,
                    caption=caption,
                    content_hash="d" * 64,
                    items=[PostItem(position=0, media_id=sample["media_id"])],
                )
            )
    log_in(client, clock)

    page = client.get(f"{sample['base']}/posts").text

    published, folded = (
        page.index("A caption written by the operator"),
        page.index('<details class="folded'),
    )
    assert published < folded < page.index("A draft in progress") < page.index("Called off")


def test_missing_post_and_media_are_not_found(client, clock, enrolled, connected):
    log_in(client, clock)

    assert client.get(f"{connected}/posts/nope").status_code == 404
    assert client.get(f"{connected}/stats/nope").status_code == 404
    assert client.get(f"{connected}/media/nope").status_code == 404


def test_deleted_media_file_shows_no_thumbnail(app, client, clock, sample, settings):
    (settings.media_dir / "photo.jpeg").unlink()
    log_in(client, clock)
    base = sample["base"]

    assert f"{base}/media/" not in client.get(f"{base}/posts").text
    assert client.get(f"{base}/media/{sample['media_id']}").status_code == 404


def test_login_form_body_is_capped(client):
    response = client.post(
        "/ui/login",
        content=b"password=" + b"a" * 5000,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 413


# Command line


@pytest.fixture
def cli_env(settings, monkeypatch):
    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(settings.database_path))
    monkeypatch.setenv("JORGEX_SOCIAL_SECRET_KEY_FILE", str(settings.secret_key_file))
    return settings


def _stdin(monkeypatch, value: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(value))


def test_cli_creates_the_operator_and_enrolls_totp(app, cli_env, monkeypatch, capsys):
    _stdin(monkeypatch, PASSWORD + "\n")
    main(["create-operator"])

    monkeypatch.setattr(operator, "new_totp_secret", lambda: TOTP_SECRET)
    _stdin(monkeypatch, code_at(datetime.now(UTC)) + "\n")
    main(["enroll-totp"])

    out = capsys.readouterr().out
    assert "otpauth://totp/jorgex-social:operator?secret=" + TOTP_SECRET in out
    with app.state.read_sessionmaker() as session:
        stored = session.scalar(select(Operator))
    assert TOTP_SECRET.encode() not in stored.totp_secret_encrypted
    assert stored.password_hash.startswith("$argon2id$")
    key = crypto.load_key(cli_env.secret_key_file)
    assert crypto.decrypt(key, stored.totp_secret_encrypted, operator.TOTP_CONTEXT) == TOTP_SECRET


def test_cli_enrolment_with_a_wrong_code_stores_nothing(app, cli_env, monkeypatch):
    _stdin(monkeypatch, PASSWORD + "\n")
    main(["create-operator"])
    monkeypatch.setattr(operator, "new_totp_secret", lambda: TOTP_SECRET)
    _stdin(monkeypatch, wrong_code(datetime.now(UTC)) + "\n")

    with pytest.raises(SystemExit, match="does not match"):
        main(["enroll-totp"])

    assert rows(app, "SELECT totp_secret_encrypted FROM operator") == [(None,)]


def test_cli_refuses_a_short_password_and_a_second_operator(cli_env, monkeypatch):
    _stdin(monkeypatch, "short\n")
    with pytest.raises(SystemExit, match="at least 12"):
        main(["create-operator"])

    _stdin(monkeypatch, PASSWORD + "\n")
    main(["create-operator"])
    _stdin(monkeypatch, PASSWORD + "\n")
    with pytest.raises(SystemExit, match="already exists"):
        main(["create-operator"])


# Review fixes


@pytest.mark.usefixtures("enrolled")
@pytest.mark.parametrize("code", ["１２３４５６", "é12345", "12345６", "1234567", " 12 3", "12345"])
def test_a_malformed_code_fails_without_argon2_or_a_write(app, client, clock, code, monkeypatch):
    def no_argon2(*_):
        raise AssertionError("a malformed code paid for a password check")

    def no_writes():
        raise AssertionError("a malformed code opened a write transaction")

    monkeypatch.setattr(operator, "_password_matches", no_argon2)
    monkeypatch.setattr(app.state, "sessionmaker", no_writes)

    response = log_in(client, clock, code=code)

    assert response.status_code == 401
    assert INVALID in response.text


def test_enrolment_refuses_a_non_ascii_code(app, settings, clock):
    with app.state.sessionmaker() as session, session.begin():
        operator.create_operator(session, PASSWORD, clock.now())
    key = crypto.load_key(settings.secret_key_file)

    with (
        pytest.raises(operator.OperatorError, match="does not match"),
        app.state.sessionmaker() as session,
        session.begin(),
    ):
        operator.enroll_totp(session, key, TOTP_SECRET, "１２３４５６", clock.now())


def test_logout_with_an_unknown_cookie_does_not_take_the_write_lock(app, client, monkeypatch):
    def no_writes():
        raise AssertionError("signing out a made-up session opened a write transaction")

    monkeypatch.setattr(app.state, "sessionmaker", no_writes)

    response = client.post(
        "/ui/logout", headers={"cookie": f"{COOKIE}=made-up"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_password_checks_run_two_at_a_time_at_most(monkeypatch):
    import threading
    import time

    running, peak, lock = 0, 0, threading.Lock()

    def slow_verify(password_hash, password):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return True

    class SlowHasher:
        verify = staticmethod(slow_verify)

    monkeypatch.setattr(operator, "_hasher", SlowHasher())
    threads = [
        threading.Thread(target=operator._password_matches, args=("hash", "pw")) for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == 2


def test_next_scheduled_is_ordered_and_limited_by_schedule_in_the_query(app, clock, sample):
    from jorgex_social.publishing.api import next_scheduled

    with app.state.sessionmaker() as session, session.begin():
        # Created after the sample's post (due in a day) yet due sooner, and one due later.
        account = scope.current_account(session, app.state.network)
        for hours, caption in ((2, "Due soonest"), (48, "Due last")):
            session.add(
                Post(
                    account_id=account.id,
                    type="image",
                    status="scheduled",
                    caption=caption,
                    content_hash="e" * 64,
                    scheduled_for=clock.now() + timedelta(hours=hours),
                    items=[PostItem(position=0, media_id=sample["media_id"])],
                )
            )

    with app.state.read_sessionmaker() as session:
        account_id = scope.account_id_of(session, app.state.network)
        found = next_scheduled(session, account_id, limit=2)

    assert [post["caption"] for post in found] == ["Due soonest", "Next one"]


@pytest.mark.usefixtures("enrolled")
def test_a_login_form_with_too_many_fields_is_a_bad_request(client):
    body = "&".join(f"f{i}=x" for i in range(20))

    response = client.post(
        "/ui/login",
        content=body.encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400


# A right password with a wrong code


def fail_the_code(client, clock, times=1):
    for _ in range(times):
        response = log_in(client, clock, code=wrong_code(clock.now()))
        assert response.status_code == 401
    return response


def password_incidents(app):
    with app.state.read_sessionmaker() as session:
        return session.scalars(
            select(ChannelIncident).where(ChannelIncident.type == "sign_in_password_accepted")
        ).all()


def login_details(app):
    found = rows(
        app,
        "SELECT outcome, detail FROM audit_log WHERE action = 'operator.login' "
        "ORDER BY occurred_at, rowid",
    )
    return [(outcome, json.loads(detail)) for outcome, detail in found]


@pytest.mark.usefixtures("enrolled")
def test_the_third_wrong_code_opens_the_incident_and_the_answer_never_changes(app, client, clock):
    wrong_password = log_in(client, clock, password="not the password at all")

    answers = [fail_the_code(client, clock), fail_the_code(client, clock)]
    assert password_incidents(app) == []

    answers.append(fail_the_code(client, clock))
    [incident] = password_incidents(app)

    assert incident.detail == {"wrong_codes": 3}
    assert incident.account_id is None  # the operator's, not an account's
    assert incident.closed_at is None
    for answer in answers:
        assert answer.status_code == wrong_password.status_code == 401
        assert answer.text == wrong_password.text


@pytest.mark.usefixtures("enrolled")
def test_wrong_passwords_do_not_count_as_wrong_codes(app, client, clock):
    for _ in range(4):
        log_in(client, clock, password="not the password at all")

    assert password_incidents(app) == []


@pytest.mark.usefixtures("enrolled")
def test_a_successful_sign_in_resets_the_count(app, client, clock):
    fail_the_code(client, clock, times=2)
    clock.advance(minutes=1)
    assert log_in(client, clock).status_code == 303
    clock.advance(minutes=1)

    fail_the_code(client, clock, times=2)
    assert password_incidents(app) == []

    fail_the_code(client, clock)
    assert len(password_incidents(app)) == 1


@pytest.mark.usefixtures("enrolled")
def test_wrong_codes_older_than_an_hour_do_not_count(app, client, clock):
    fail_the_code(client, clock, times=2)
    clock.advance(hours=1)

    fail_the_code(client, clock, times=2)
    assert password_incidents(app) == []

    fail_the_code(client, clock)
    assert password_incidents(app)[0].detail == {"wrong_codes": 3}


@pytest.mark.usefixtures("enrolled")
def test_signing_in_does_not_close_the_incident_and_the_cli_set_password_does(
    app, client, clock, cli_env, monkeypatch
):
    fail_the_code(client, clock, times=3)
    clock.advance(minutes=1)
    assert log_in(client, clock).status_code == 303
    assert password_incidents(app)[0].closed_at is None

    _stdin(monkeypatch, "another long password\n")
    main(["set-password"])

    assert password_incidents(app)[0].closed_at is not None


@pytest.mark.usefixtures("enrolled")
def test_enrolling_the_second_factor_again_closes_the_incident(app, settings, client, clock):
    fail_the_code(client, clock, times=3)
    clock.advance(minutes=5)
    key = crypto.load_key(settings.secret_key_file)
    with app.state.sessionmaker() as session, session.begin():
        operator.enroll_totp(session, key, TOTP_SECRET, code_at(clock.now()), clock.now())

    assert password_incidents(app)[0].closed_at is not None


@pytest.mark.usefixtures("enrolled")
def test_wrong_codes_from_before_a_password_change_do_not_reopen_the_incident(app, client, clock):
    fail_the_code(client, clock, times=3)
    clock.advance(minutes=1)
    new_password = "another long password"
    with app.state.sessionmaker() as session, session.begin():
        operator.set_password(session, new_password, clock.now())
    clock.advance(minutes=1)

    assert (
        log_in(client, clock, password=new_password, code=wrong_code(clock.now())).status_code
        == 401
    )

    assert [incident.closed_at is not None for incident in password_incidents(app)] == [True]


# The audit of signing in


@pytest.mark.usefixtures("enrolled")
def test_the_audit_names_the_client_and_only_a_right_password_has_a_reason(app, clock):
    client = TestClient(app, base_url=LOCAL, headers={"origin": LOCAL}, client=("198.51.100.7", 1))

    log_in(client, clock, password="not the password at all")
    fail_the_code(client, clock)
    assert log_in(client, clock).status_code == 303

    assert login_details(app) == [
        ("denied", {"client": "198.51.100.7"}),
        ("denied", {"client": "198.51.100.7", "reason": "wrong_code"}),
        ("ok", {"client": "198.51.100.7"}),
    ]


@pytest.mark.usefixtures("enrolled")
def test_a_successful_sign_in_purges_only_old_denied_sign_ins(app, client, clock):
    log_in(client, clock, password="purged after 90 days")
    assert log_in(client, clock).status_code == 303
    clock.advance(days=2)
    fail_the_code(client, clock)
    other_actions = rows(app, "SELECT count(*) FROM audit_log WHERE action != 'operator.login'")
    clock.advance(days=88)

    # Anyone can make a denial happen, so none of them pays for the purge.
    log_in(client, clock, password="another wrong password")
    fail_the_code(client, clock)
    assert [outcome for outcome, _ in login_details(app)] == [
        "denied",
        "ok",
        "denied",
        "denied",
        "denied",
    ]

    assert log_in(client, clock).status_code == 303

    assert [outcome for outcome, _ in login_details(app)] == [
        "ok",
        "denied",
        "denied",
        "denied",
        "ok",
    ]
    assert (
        rows(app, "SELECT count(*) FROM audit_log WHERE action != 'operator.login'")
        == other_actions
    )


@pytest.mark.usefixtures("enrolled")
def test_only_denied_sign_ins_can_be_deleted_from_the_audit_log(app, client, clock):
    assert log_in(client, clock).status_code == 303

    for action in ("operator.login", "operator.created"):
        with pytest.raises(Exception, match="append-only"), app.state.sessionmaker() as session:
            session.execute(text("DELETE FROM audit_log WHERE action = :a"), {"a": action})


# Password managers


@pytest.mark.usefixtures("enrolled")
def test_the_form_gives_password_managers_a_username_the_server_ignores(client, clock):
    page = client.get("/ui/login").text
    assert 'name="username" value="operator" autocomplete="username" hidden' in page

    fields = {
        "username": "not checked",
        "password": PASSWORD,
        "code": code_at(clock.now()),
        "next": "/ui/posts",
    }
    response = client.post("/ui/login", data=fields, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/posts"


# Settings: appearance

SETTINGS = "/ui/settings/appearance"
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 32
ICO = b"\x00\x00\x01\x00\x01\x00" + b"i" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
NO_FILE = {"icon": ("", b"")}


def save(client, *, files=None, **fields):
    data = {"name": "Social", "name_highlight": "", "css": "", **fields}
    return client.post(SETTINGS, data=data, files=files or NO_FILE, follow_redirects=False)


def appearance_audit(app):
    return rows(app, "SELECT actor_type, detail FROM audit_log WHERE action='appearance.updated'")


@pytest.fixture
def signed_in(client, clock, enrolled):
    assert log_in(client, clock).status_code == 303
    return client


def test_settings_goes_to_its_first_section(signed_in):
    response = signed_in.get("/ui/settings", follow_redirects=False)

    assert (response.status_code, response.headers["location"]) == (303, SETTINGS)
    page = signed_in.get(SETTINGS).text
    assert '<a href="/ui/settings/appearance" aria-current="page">Appearance</a>' in page


@pytest.mark.parametrize("path", ["/ui/settings", SETTINGS])
def test_settings_with_an_old_sign_in_asks_for_credentials_and_returns(signed_in, clock, path):
    clock.advance(hours=1)

    response = signed_in.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login?" + urlencode({"next": path})
    assert "set-cookie" not in response.headers  # the session still serves the other pages
    assert signed_in.get("/ui/").status_code == 200
    back = signed_in.post(
        "/ui/login",
        data={"password": PASSWORD, "code": code_at(clock.now()), "next": path},
        follow_redirects=False,
    )
    assert back.headers["location"] == path
    assert signed_in.get(SETTINGS).status_code == 200


def test_an_old_sign_in_cannot_save(app, signed_in, clock):
    clock.advance(hours=1)

    response = save(signed_in, name="Other")

    assert response.status_code == 303
    # Back to Settings once signed in again, not Home; what was typed is not kept.
    assert response.headers["location"] == "/ui/login?" + urlencode({"next": SETTINGS})
    assert "Other" not in signed_in.get("/ui/login").text
    assert appearance_audit(app) == []


def test_a_post_without_a_session_gets_no_return_path(client):
    response = save(client, name="Other")

    assert response.headers["location"] == "/ui/login"


def test_the_page_warns_that_saving_needs_a_recent_sign_in(signed_in):
    assert "Saving requires a sign-in under an hour old" in signed_in.get(SETTINGS).text


def test_a_foreign_origin_cannot_save(app, signed_in):
    refused = signed_in.post(
        SETTINGS,
        data={"name": "Other"},
        files=NO_FILE,
        headers={"origin": "https://evil.example"},
    )

    assert refused.status_code == 403
    assert appearance_audit(app) == []


def test_what_is_saved_shows_on_the_sign_in_page(app, signed_in):
    saved = save(
        signed_in,
        name="  JorgeX Social ",
        name_highlight="X",
        css="body { color: red; }",
        files={"icon": ("logo.png", PNG)},
    )

    assert (saved.status_code, saved.headers["location"]) == (303, f"{SETTINGS}?saved=1")
    assert "Saved." in signed_in.get(saved.headers["location"]).text
    visitor = TestClient(app, base_url=LOCAL)
    page = visitor.get("/ui/login").text
    assert 'Jorge<span class="accent">X</span> Social' in page
    assert '<img class="wordmark-icon" src="/ui/icon"' in page
    assert '<link rel="icon" href="/ui/icon">' in page
    assert page.index("/ui/static/style.css") < page.index('href="/ui/appearance.css"')
    assert "<title>Sign in · JorgeX Social</title>" in page
    css = visitor.get("/ui/appearance.css")
    assert css.text == "body { color: red; }"
    assert css.headers["content-type"].startswith("text/css")
    assert css.headers["x-content-type-options"] == "nosniff"
    assert (visitor.get("/ui/icon").content, visitor.get("/ui/icon").headers["content-type"]) == (
        PNG,
        "image/png",
    )


def test_the_default_name_replaces_any_brand(app, signed_in):
    for page in (TestClient(app, base_url=LOCAL).get("/ui/login").text, signed_in.get("/ui/").text):
        assert "<title>" in page
        assert "· Social</title>" in page
        assert "Jorge" not in page
        assert 'class="accent"' not in page


def test_the_header_links_a_menu_that_signs_out(signed_in):
    page = signed_in.get("/ui/").text
    menu = page[page.index('<details class="profile-menu">') : page.index("</details>")]

    assert 'aria-label="Account menu"' in menu
    assert menu.index('href="/ui/settings"') < menu.index('action="/ui/logout"')
    assert ">Settings</a>" not in page.replace(menu, "")
    assert signed_in.post("/ui/logout", follow_redirects=False).status_code == 303
    assert signed_in.get("/ui/", follow_redirects=False).headers["location"] == "/ui/login"


def test_the_menu_is_not_on_the_sign_in_page(client):
    assert "profile-menu" not in client.get("/ui/login").text


@pytest.mark.parametrize("path", ["/ui/appearance.css", "/ui/icon"])
def test_public_appearance_revalidates(app, signed_in, path):
    visitor = TestClient(app, base_url=LOCAL)
    first = visitor.get(path)
    assert first.headers["cache-control"] == "no-cache"

    etag = first.headers["etag"]
    same = visitor.get(path, headers={"if-none-match": f'"other", W/{etag}'})
    assert (same.status_code, same.content) == (304, b"")
    assert same.headers["etag"] == etag

    save(signed_in, css="a { color: red; }", files={"icon": ("x.png", PNG)})
    assert visitor.get(path, headers={"if-none-match": etag}).status_code == 200


def test_public_appearance_takes_no_write_lock(app, monkeypatch):
    def forbidden():
        raise AssertionError("a public appearance route opened a write session")

    client = TestClient(app, base_url=LOCAL)
    monkeypatch.setattr(app.state, "sessionmaker", forbidden)

    assert client.get("/ui/appearance.css").text == ""
    assert client.get("/ui/icon").status_code == 200


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (PNG, "image/png"),
        (ICO, "image/x-icon"),
        (SVG, "image/svg+xml"),
        (b"\xef\xbb\xbf<?xml version='1.0'?>\n<!-- c --><!DOCTYPE svg>\n<svg/>", "image/svg+xml"),
    ],
)
def test_the_icon_type_comes_from_its_content(app, signed_in, data, kind):
    # The name and the part's Content-Type both say something else.
    files = {"icon": ("notes.txt", data, "text/plain")}

    assert save(signed_in, files=files).status_code == 303

    icon = TestClient(app, base_url=LOCAL).get("/ui/icon")
    assert (icon.content, icon.headers["content-type"]) == (data, kind)


@pytest.mark.parametrize(
    "data",
    [
        b"just some text",
        b"<svgish></svgish>",
        b"<html><svg></svg></html>",
        b"\x00\x00\x01\x00\x00\x00",
        b"\xff\xd8\xff\xe0 a jpeg",
    ],
)
def test_anything_else_is_refused_and_the_form_keeps_what_was_entered(signed_in, data):
    refused = save(
        signed_in,
        name="Kept",
        css="a { color: red; }",
        files={"icon": ("logo.png", data, "image/png")},
    )

    assert refused.status_code == 422
    assert "The icon must be a PNG, ICO or SVG file." in refused.text
    assert 'value="Kept"' in refused.text
    assert "a { color: red; }</textarea>" in refused.text
    assert "· Social</title>" in refused.text  # nothing saved
    assert refused.headers["content-security-policy"].startswith("default-src 'none'")


def test_limits(signed_in):
    limit = appearance.MAX_CSS_BYTES

    assert save(signed_in, css="a" * limit).status_code == 303
    assert "The CSS is over 256 KB." in save(signed_in, css="a" * (limit + 1)).text
    # Bytes, not characters.
    assert save(signed_in, css="é" * (limit // 2 + 1)).status_code == 422
    fits = PNG + b"p" * (appearance.MAX_ICON_BYTES - len(PNG))
    assert save(signed_in, files={"icon": ("a.png", fits)}).status_code == 303
    too_big = save(signed_in, files={"icon": ("a.png", fits + b"p")})
    assert "The icon is over 256 KB." in too_big.text
    assert save(signed_in, name="n" * 40).status_code == 303
    assert "The name must be 1 to 40 characters." in save(signed_in, name="n" * 41).text


@pytest.mark.parametrize("name", ["", "   "])
def test_an_empty_name_is_refused(signed_in, name):
    refused = save(signed_in, name=name)

    assert refused.status_code == 422
    assert "The name must be 1 to 40 characters." in refused.text


OVER_THE_CAP = b"name=" + b"a" * (1024 * 1024)
URLENCODED = {"content-type": "application/x-www-form-urlencoded"}


def _chunks():
    yield OVER_THE_CAP[:1000]
    yield OVER_THE_CAP[1000:]


@pytest.mark.parametrize(
    "request_args",
    [
        pytest.param({"content": OVER_THE_CAP, "headers": URLENCODED}, id="declared"),
        pytest.param({"content": _chunks(), "headers": URLENCODED}, id="absent"),
        pytest.param(
            {"content": OVER_THE_CAP, "headers": {**URLENCODED, "content-length": "10"}},
            id="false",
        ),
    ],
)
def test_a_body_over_one_megabyte_is_refused(app, signed_in, request_args):
    response = signed_in.post(SETTINGS, **request_args)

    assert response.status_code == 413
    assert appearance_audit(app) == []


def test_a_form_at_the_limits_of_css_and_icon_fits_the_body_cap(signed_in):
    icon = PNG + b"p" * (appearance.MAX_ICON_BYTES - len(PNG))

    saved = save(signed_in, css="a" * appearance.MAX_CSS_BYTES, files={"icon": ("a.png", icon)})

    assert saved.status_code == 303


def test_name_and_highlight_are_escaped_piece_by_piece(app, signed_in):
    assert save(signed_in, name="<b>&<i>", name_highlight="&").status_code == 303

    page = TestClient(app, base_url=LOCAL).get("/ui/login").text

    assert '&lt;b&gt;<span class="accent">&amp;</span>&lt;i&gt;' in page
    assert "<b>" not in page
    assert "<title>Sign in · &lt;b&gt;&amp;&lt;i&gt;</title>" in page


def test_only_the_first_occurrence_is_highlighted(app, signed_in):
    assert save(signed_in, name="XaX", name_highlight="X").status_code == 303

    page = TestClient(app, base_url=LOCAL).get("/ui/login").text

    assert '<span class="accent">X</span>aX' in page


@pytest.mark.parametrize(
    ("name", "highlight"),
    [("JorgeX", "x"), ("JorgeX", "Y"), ("Social", " Social")],
)
def test_a_highlight_not_in_the_name_is_refused(signed_in, name, highlight):
    refused = save(signed_in, name=name, name_highlight=highlight)

    assert refused.status_code == 422
    assert "The highlight must be part of the name" in refused.text
    assert f'value="{highlight}"' in refused.text


def test_the_form_can_change_name_and_highlight_together(signed_in):
    save(signed_in, name="JorgeX", name_highlight="X")

    assert save(signed_in, name="Acme", name_highlight="me").status_code == 303

    assert 'Ac<span class="accent">me</span>' in signed_in.get(SETTINGS).text


def test_the_icon_cannot_be_replaced_and_removed_at_once(signed_in):
    save(signed_in, files={"icon": ("a.png", PNG)})

    refused = save(signed_in, clear_icon="on", files={"icon": ("b.png", ICO)})

    assert refused.status_code == 422
    assert "not both" in refused.text


def test_removing_the_icon_brings_the_default_back(app, signed_in):
    default = TestClient(app, base_url=LOCAL).get("/ui/icon").content
    save(signed_in, files={"icon": ("a.png", PNG)})

    assert 'name="clear_icon"' in signed_in.get(SETTINGS).text
    assert save(signed_in, clear_icon="on").status_code == 303

    assert TestClient(app, base_url=LOCAL).get("/ui/icon").content == default


def test_reset_returns_the_defaults_and_audits_without_content(app, signed_in):
    default = TestClient(app, base_url=LOCAL).get("/ui/icon").content
    save(
        signed_in,
        name="Secret Brand",
        name_highlight="Brand",
        css="a { color: red; }",
        files={"icon": ("a.png", PNG)},
    )

    # The button's form also sends the other fields, even invalid: they do not count.
    assert save(signed_in, reset="1", name="").status_code == 303

    visitor = TestClient(app, base_url=LOCAL)
    assert "· Social</title>" in visitor.get("/ui/login").text
    assert visitor.get("/ui/appearance.css").text == ""
    assert visitor.get("/ui/icon").content == default
    entries = appearance_audit(app)
    assert [(actor, json.loads(detail)) for actor, detail in entries] == [
        ("operator", {"changed": ["name", "name_highlight", "icon", "css"]}),
        ("operator", {"changed": ["name", "name_highlight", "icon", "css"]}),
    ]
    assert "Secret" not in str(rows(app, "SELECT * FROM audit_log"))


def test_only_real_changes_are_audited(app, signed_in):
    save(signed_in, name="Acme", css="a { color: red; }\r\n")
    # What a browser sends back unchanged: its textarea uses CRLF.
    assert save(signed_in, name="Acme", css="a { color: red; }\r\n").status_code == 303

    assert len(appearance_audit(app)) == 1


def test_a_directly_opened_svg_icon_cannot_run_its_script(app, signed_in):
    save(signed_in, files={"icon": ("a.svg", SVG)})

    icon = TestClient(app, base_url=LOCAL).get("/ui/icon")

    assert icon.content == SVG
    assert icon.headers["content-type"] == "image/svg+xml"
    assert icon.headers["x-content-type-options"] == "nosniff"
    assert icon.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    )


def test_the_default_icon_carries_the_same_headers_and_no_letter(client):
    icon = client.get("/ui/icon")

    assert icon.headers["content-type"] == "image/svg+xml"
    assert "sandbox" in icon.headers["content-security-policy"]
    assert b"<text" not in icon.content


def test_the_sign_in_page_survives_an_unreadable_setting(app, client, caplog):
    with app.state.sessionmaker() as session, session.begin():
        session.execute(text("DROP TABLE settings"))

    response = client.get("/ui/login")

    assert response.status_code == 200
    assert "· Social</title>" in response.text
    assert "could not read the product name" in caplog.text


# Everything can be themed


def _stylesheet() -> str:
    return (Path(appearance.__file__).parent.parent / "ui" / "static" / "style.css").read_text()


def _outside_root(css: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.sub(r"^:root \{.*?^\}", "", css, flags=re.M | re.S)


LITERAL_COLOUR = re.compile(r"(?<!&)#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|oklch|lab)\(")
COLOUR_WORDS = re.compile(
    r"\b(?:black|white|gray|grey|red|green|blue|yellow|orange|purple|pink|cyan|magenta|silver"
    r"|maroon|navy|teal|olive|lime|aqua|fuchsia)\b"
)
COLOUR_PROPERTY = re.compile(
    r"(?:^|[;{\s])((?:background|color|fill|stroke|border|outline|box-shadow|text-shadow"
    r"|caret-color|accent-color)[\w-]*)\s*:([^;}]*)",
    re.M,
)
TEMPLATES = Path(appearance.__file__).parent.parent / "ui" / "templates"


def test_the_stylesheet_has_no_literal_colour_outside_its_tokens():
    css = _outside_root(_stylesheet())

    assert LITERAL_COLOUR.findall(css) == []
    declarations = COLOUR_PROPERTY.findall(css)
    assert declarations  # the pattern still matches something
    assert [d for d in declarations if COLOUR_WORDS.search(d[1])] == []


def test_every_variable_the_stylesheet_uses_is_a_token():
    used = set(re.findall(r"var\((--[\w-]+)", _stylesheet()))

    # A pill's own parameter: each pill class sets it from a token.
    assert used - {"--tone"} <= set(appearance.VARIABLES)
    assert set(appearance.VARIABLES) <= used


def test_templates_and_charts_paint_only_through_the_stylesheet():
    chart = TEMPLATES.parent / "chart.py"
    for path in [*TEMPLATES.iterdir(), chart]:
        source = path.read_text()
        assert "style=" not in source, path
        assert LITERAL_COLOUR.findall(source) == [], path
        painted = re.findall(r"""\b(?:fill|stroke)=["']([^"']*)""", source)
        assert set(painted) <= {"currentColor", "none"}, path


def test_the_tokens_keep_the_default_look():
    assert {
        name: appearance.VARIABLES[name]
        for name in ("--bg", "--accent", "--accent-hover", "--accent-2", "--on-accent", "--shadow")
    } == {
        "--bg": "#101113",
        "--accent": "#22d3ee",
        "--accent-hover": "#67e8f9",
        "--accent-2": "#a78bfa",
        "--on-accent": "#101113",
        "--shadow": "#000000",
    }


def test_the_page_lists_exactly_the_tokens_and_a_complete_light_theme(signed_in):
    page = signed_in.get(SETTINGS).text

    listed = re.findall(r'<td class="mono">(--[\w-]+)</td>', page)
    assert listed == list(appearance.VARIABLES)
    assert '<td class="mono">--accent</td><td class="mono">#22d3ee</td>' in page
    assert "--cyan-400" not in page
    assert ".wordmark-icon" in page and ".wordmark .accent" in page
    # The example changes every colour, so nothing dark is left behind on a light page.
    colours = {name for name, value in appearance.VARIABLES.items() if value.startswith("#")}
    assert colours <= set(re.findall(r"(--[\w-]+):", appearance.LIGHT_THEME))
    assert "color-scheme: light;" in appearance.LIGHT_THEME
    assert appearance.warnings(appearance.LIGHT_THEME) == []


def test_an_unknown_custom_property_is_saved_with_a_notice(app, signed_in):
    css = """
    /* --in-a-comment: 1; */
    :root { --colr-bg: red; --accent: blue; --colr-bg: pink; }
    a { color: var(--not-declared-here); }
    .x { --tone: red; }
    """

    saved = save(signed_in, css=css)

    assert saved.status_code == 303
    page = signed_in.get(saved.headers["location"]).text
    assert page.count('class="notice warning"') == 1
    assert "--colr-bg is not a variable of this interface and does nothing" in page
    assert TestClient(app, base_url=LOCAL).get("/ui/appearance.css").text.strip().startswith("/*")


def test_the_page_of_a_refused_save_reports_on_the_css_entered(signed_in):
    refused = save(signed_in, name="", css=":root { --colr-bg: red; }")

    assert refused.status_code == 422
    assert "--colr-bg is not a variable" in refused.text


def test_cli_reset_appearance_recovers_and_audits(app, signed_in, cli_env, capsys):
    save(signed_in, name="Acme", css="body { display: none; }")
    capsys.readouterr()

    main(["reset-appearance"])

    assert json.loads(capsys.readouterr().out) == {"reset": ["name", "css"]}
    visitor = TestClient(app, base_url=LOCAL)
    assert "· Social</title>" in visitor.get("/ui/login").text
    assert visitor.get("/ui/appearance.css").text == ""
    (actor, detail) = appearance_audit(app)[-1]
    assert (actor, json.loads(detail)) == ("operator", {"changed": ["name", "css"]})


def _record_reads_on_the_loop(app, monkeypatch) -> list[str]:
    """Blocking on the event loop stalls every endpoint, so no read may start on it."""
    on_the_loop = []
    opened = app.state.read_sessionmaker

    def watched():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return opened()
        on_the_loop.append("read session opened on the event loop")
        return opened()

    monkeypatch.setattr(app.state, "read_sessionmaker", watched)
    return on_the_loop


@pytest.mark.usefixtures("enrolled")
def test_a_failed_sign_in_reads_nothing_on_the_event_loop(app, client, clock, monkeypatch):
    on_the_loop = _record_reads_on_the_loop(app, monkeypatch)

    response = log_in(client, clock, password="wrong password!!")

    assert response.status_code == 401
    assert "· Social</title>" in response.text
    assert on_the_loop == []


@pytest.mark.usefixtures("enrolled")
def test_a_busy_sign_in_reads_nothing_on_the_event_loop(app, client, clock, monkeypatch):
    def busy(*_):
        raise operator.Busy

    monkeypatch.setattr(operator, "log_in", busy)
    on_the_loop = _record_reads_on_the_loop(app, monkeypatch)

    response = log_in(client, clock)

    assert response.status_code == 503
    assert on_the_loop == []


def test_the_saved_name_shows_on_a_failed_sign_in(app, signed_in, clock):
    save(signed_in, name="Acme")
    clock.advance(minutes=1)

    response = log_in(TestClient(app, base_url=LOCAL, headers={"origin": LOCAL}), clock, "wrong!!")

    assert response.status_code == 401
    assert "· Acme</title>" in response.text


# Two accounts: every page works on the one in its URL

FIRST = "17840000000000001"
SECOND = "17840000000000002"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


@pytest.fixture
def two(app, settings, clock, enrolled):
    """Two connected accounts of the mode, the first one older, each with a post and a photo, a
    rule with an event and its reply, an incident and a day of stats. Every page has something of
    its own to show, and something of the other account it must not."""
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    (settings.media_dir / "photo.jpeg").write_bytes(JPEG)
    now = clock.now()
    day = datetime(2026, 10, 29, tzinfo=UTC)
    with app.state.sessionmaker() as session, session.begin():
        first = scope.current_account(session, app.state.network)
        first.external_account_id, first.username = FIRST, "first_account"
        second = Connection(
            network=app.state.network, external_account_id=SECOND, username="second_account"
        )
        session.add(second)
        session.flush()
        found = {}
        for account, piece, reach, followers in (
            (first, "first", 301, 1201),
            (second, "second", 601, 2201),
        ):
            media = Media(
                account_id=account.id,
                relative_path="photo.jpeg",
                sha256="a" * 64,
                type="jpeg",
                bytes=len(JPEG),
            )
            session.add(media)
            session.flush()
            post = Post(
                account_id=account.id,
                type="image",
                status="published",
                caption=f"A piece of the {piece}",
                content_hash="b" * 64,
                published_at=now,
                items=[PostItem(position=0, media_id=media.id)],
            )
            session.add(post)
            session.flush()
            rule = Rule(
                account_id=account.id,
                name=f"Rule of the {piece} account",
                channel="comment",
                priority=1,
                active=True,
                activated_at=now,
                content={
                    "keywords": [f"word of the {piece}"],
                    "private_replies": [f"Reply of the {piece}"],
                },
                created_at=now,
                updated_at=now,
            )
            session.add(rule)
            session.flush()
            event = Event(
                account_id=account.id,
                type="comment",
                external_ref=f"comment-of-the-{piece}",
                occurred_at=now,
                received_at=now,
                rule_id=rule.id,
            )
            session.add(event)
            session.flush()
            session.add(
                Delivery(
                    account_id=account.id,
                    event_id=event.id,
                    rule_id=rule.id,
                    action="private_reply",
                    status="sent",
                    text=f"Reply of the {piece}",
                    created_at=now,
                )
            )
            session.add(
                ChannelIncident(
                    account_id=account.id,
                    type="credential_expiring",
                    opened_at=now - timedelta(hours=2),
                    detail={"who": f"the {piece} account"},
                )
            )
            session.add(
                AccountSnapshot(
                    account_id=account.id,
                    period_start=day,
                    period_end=day + timedelta(days=1),
                    taken_at=now,
                    followers=followers,
                    metrics={"reach": reach, "views": 500, "accounts_engaged": 20},
                )
            )
            found[account.external_account_id] = {
                "id": account.id,
                "base": f"/ui/a/{account.id}",
                "post": post.id,
                "media": media.id,
            }
        # The operator's own: no account, so every account sees it, by design.
        session.add(
            ChannelIncident(
                account_id=None,
                type="sign_in_password_accepted",
                opened_at=now - timedelta(minutes=5),
                detail={"failed": 3},
            )
        )
    return {"first": found[FIRST], "second": found[SECOND]}


@pytest.fixture
def foreign(app):
    """Rows no page may work on: an account of the other mode, and one that is not connected yet."""
    with app.state.sessionmaker() as session, session.begin():
        other_mode = Connection(
            network="instagram", external_account_id=FIRST, username="real_account"
        )
        pending = Connection(network=app.state.network, external_account_id="", username="")
        session.add_all([other_mode, pending])
        session.flush()
        return {"other_mode": other_mode.id, "pending": pending.id}


def test_with_nothing_connected_the_home_needs_no_account_in_the_url(client, clock, enrolled):
    log_in(client, clock)

    response = client.get("/ui/", follow_redirects=False)

    assert response.status_code == 200
    assert "No account connected" in response.text
    assert "account-switcher" not in response.text
    assert "account-chip" not in response.text


def test_the_home_opens_the_account_used_last(client, clock, two):
    log_in(client, clock)
    first, second = two["first"], two["second"]

    assert client.get("/ui/", follow_redirects=False).headers["location"] == f"{first['base']}/"

    visited = client.get(f"{second['base']}/")

    response = client.get("/ui/", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, f"{second['base']}/")
    cookie = visited.headers["set-cookie"].lower()
    assert f"{ACCOUNT_COOKIE}={second['id']}" in cookie
    # A preference with no power over a request, so it carries the same flags as the session and
    # no script can read it.
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/ui" in cookie
    assert f"max-age={int(operator.SESSION_LIFETIME.total_seconds())}" in cookie
    assert "secure" not in cookie  # loopback over plain http, like the session cookie


def test_a_cookie_that_names_an_account_it_cannot_open_falls_back_to_the_first(
    client, clock, two, foreign
):
    log_in(client, clock)

    for account in (foreign["other_mode"], foreign["pending"], "made-up"):
        client.cookies.set(ACCOUNT_COOKIE, account, path="/ui")
        response = client.get("/ui/", follow_redirects=False)
        assert response.headers["location"] == f"{two['first']['base']}/", account


def test_the_home_card_switches_account(client, clock, two):
    log_in(client, clock)
    first, second = two["first"], two["second"]

    page = client.get(f"{first['base']}/").text

    assert "@first_account" in page
    # The other account is one click away, and the one being looked at says so.
    assert f'href="{second["base"]}/"' in page
    assert f'href="{first["base"]}/" aria-current="page"' in page
    # Choosing one only navigates: the switcher holds no form.
    hero = page.split('<section class="hero">')[1].split("</section>")[0]
    assert "<form" not in hero


def test_a_page_works_on_its_account_whatever_the_last_one_was(client, clock, two):
    log_in(client, clock)
    first, second = two["first"], two["second"]
    client.get(f"{second['base']}/")  # the account used last is the second one

    page = client.get(f"{first['base']}/posts").text

    assert "A piece of the first" in page
    assert "A piece of the second" not in page

    theirs = client.get(f"{second['base']}/posts").text
    assert "A piece of the second" in theirs
    assert "A piece of the first" not in theirs


def test_no_page_links_to_the_other_account_or_to_an_old_url(client, clock, two):
    log_in(client, clock)
    paths = ["/", "/posts", "/stats", "/stats/18000000000000001", "/rules", "/events", "/incidents"]

    for name in ("first", "second"):
        mine = two[name]
        other = two["second" if name == "first" else "first"]
        for path in [*paths, f"/posts/{mine['post']}"]:
            page = client.get(mine["base"] + path).text
            if path == "/":
                # The switcher is the only place that names another account, and only to open its
                # home: never a post, a page or a form of it.
                assert page.count(f"/ui/a/{other['id']}") == page.count(
                    f'href="/ui/a/{other["id"]}/"'
                )
            else:
                assert f"/ui/a/{other['id']}" not in page, (name, path)
            # An old path would open whichever account was used last, not the one this page is on.
            for section in ("/posts", "/stats", "/rules", "/events", "/incidents", "/media"):
                assert f'href="/ui{section}' not in page, (name, path, section)


def test_a_post_or_a_photo_of_another_account_is_not_found(client, clock, two):
    log_in(client, clock)
    first, second = two["first"], two["second"]

    post = client.get(f"{second['base']}/posts/{first['post']}")
    assert post.status_code == 404
    assert "There is no such post" in post.text

    media = client.get(f"{second['base']}/media/{first['media']}")
    assert media.status_code == 404
    # Still inside the account: its own links, never an old path that would open another one.
    assert f'href="{second["base"]}/posts"' in media.text

    served = client.get(f"{first['base']}/media/{first['media']}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"


def test_an_unknown_account_is_not_found(client, clock, two):
    log_in(client, clock)

    response = client.get("/ui/a/0193fa00-0000-7000-8000-000000000000/posts")

    assert response.status_code == 404
    assert "There is no such account" in response.text


def test_an_account_of_the_other_mode_or_not_connected_is_not_found(client, clock, two, foreign):
    log_in(client, clock)

    for account in (foreign["other_mode"], foreign["pending"]):
        response = client.get(f"/ui/a/{account}/")
        assert response.status_code == 404, account


def test_an_account_page_without_a_session_asks_for_credentials(client, two):
    """The session comes before the account: without one, an id is never answered with a 404."""
    invented = "/ui/a/0193fa00-0000-7000-8000-000000000000"

    for base, path in (
        (invented, "/posts"),
        (two["first"]["base"], "/posts"),
        (two["first"]["base"], "/media/whatever"),
    ):
        response = client.get(f"{base}{path}", follow_redirects=False)
        assert response.status_code == 303, (base, path)
        assert response.headers["location"].startswith("/ui/login?next="), (base, path)


def test_an_old_url_opens_the_same_page_under_the_last_account(client, clock, two):
    log_in(client, clock)
    second = two["second"]["base"]
    client.get(f"{second}/")

    for path in ("/posts", "/stats", "/rules", "/events", "/incidents", "/media/abc"):
        response = client.get(f"/ui{path}", follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, f"{second}{path}")

    kept = client.get("/ui/stats/18000000000000001?days=7", follow_redirects=False)
    assert kept.headers["location"] == f"{second}/stats/18000000000000001?days=7"


def test_an_old_url_with_nothing_connected_goes_to_the_home(client, clock, enrolled):
    log_in(client, clock)

    response = client.get("/ui/posts", follow_redirects=False)

    assert (response.status_code, response.headers["location"]) == (303, "/ui/")


def test_a_page_is_read_only_and_an_old_url_takes_no_post_at_all(client, clock, two):
    log_in(client, clock)

    # A POST to a URL without the account could only write to one the request did not name.
    assert client.post("/ui/posts").status_code == 405
    assert client.post("/ui/rules").status_code == 405
    assert client.post(f"{two['first']['base']}/posts").status_code == 405


def test_every_page_of_an_account_shows_only_its_rows(client, clock, two):
    log_in(client, clock)
    marks = {
        "first": {
            "rules": "Rule of the first account",
            "events": "comment-of-the-first",
            "incidents": "who=the first account",
            "stats": "1,201",
        },
        "second": {
            "rules": "Rule of the second account",
            "events": "comment-of-the-second",
            "incidents": "who=the second account",
            "stats": "2,201",
        },
    }

    for name, other in (("first", "second"), ("second", "first")):
        base = two[name]["base"]
        for section, mark in marks[name].items():
            page = client.get(f"{base}/{section}").text
            assert mark in page, (name, section)
            assert marks[other][section] not in page, (name, section)
        # The operator's own incidents belong to the installation, so every account sees them.
        assert "sign_in_password_accepted" in client.get(f"{base}/incidents").text


def test_signing_out_forgets_the_last_account(client, clock, two):
    log_in(client, clock)
    assert client.get(f"{two['second']['base']}/").status_code == 200

    response = client.post("/ui/logout", follow_redirects=False)

    cleared = response.headers.get_list("set-cookie")
    assert any(
        cookie.startswith(f"{ACCOUNT_COOKIE}=") and "Max-Age=0" in cookie for cookie in cleared
    )


def test_an_expired_session_forgets_the_last_account(client, clock, two):
    log_in(client, clock)
    assert client.get(f"{two['second']['base']}/").status_code == 200
    clock.advance(days=8)

    response = client.get(f"{two['first']['base']}/posts", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/login")
    cleared = response.headers.get_list("set-cookie")
    assert any(
        cookie.startswith(f"{ACCOUNT_COOKIE}=") and "Max-Age=0" in cookie for cookie in cleared
    )


def test_the_last_account_cookie_is_secure_over_https(settings, clock, two):
    public = "https://social.example.com"
    app = create_app(replace(settings, public_url=public), clock)
    client = TestClient(app, base_url=public, headers={"origin": public})
    assert log_in(client, clock).status_code == 303

    response = client.get(f"{two['first']['base']}/")

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{ACCOUNT_COOKIE}={two['first']['id']}")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/ui" in cookie
