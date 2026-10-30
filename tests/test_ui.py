import hashlib
import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from jorgex_social.access import operator
from jorgex_social.access.models import Operator, OperatorSession
from jorgex_social.access.tokens import create_token
from jorgex_social.account import crypto
from jorgex_social.account.credential import seal
from jorgex_social.account.models import ChannelIncident, Connection
from jorgex_social.api import create_app
from jorgex_social.cli import main
from jorgex_social.clock import ManualClock
from jorgex_social.publishing.models import Media, Post, PostItem, PublishAttempt
from jorgex_social.replies.models import Delivery, Event, Rule
from jorgex_social.stats.models import AccountSnapshot, PostSnapshot
from jorgex_social.ui.routes import COOKIE, INVALID, cookie_is_secure

START = datetime(2026, 10, 30, 12, 0, 10, tzinfo=UTC)
PASSWORD = "correct horse battery staple"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
PAGES = ["/ui/", "/ui/posts", "/ui/stats", "/ui/rules", "/ui/events", "/ui/incidents"]


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
            app.state.network,
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


@pytest.mark.parametrize("path", [*PAGES, "/ui/posts/any", "/ui/stats/any", "/ui/media/any"])
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
def test_idle_session_expires_after_an_hour(client, clock):
    log_in(client, clock)

    clock.advance(minutes=59)
    assert client.get("/ui/", follow_redirects=False).status_code == 200  # activity recorded
    clock.advance(minutes=59)
    assert client.get("/ui/", follow_redirects=False).status_code == 200
    clock.advance(minutes=61)
    response = client.get("/ui/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


@pytest.mark.usefixtures("enrolled")
def test_session_expires_after_twelve_hours_even_when_active(client, clock):
    log_in(client, clock)
    for _ in range(14):  # 11 h 40 min of activity
        clock.advance(minutes=50)
        assert client.get("/ui/", follow_redirects=False).status_code == 200

    clock.advance(minutes=21)

    assert client.get("/ui/", follow_redirects=False).status_code == 303


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
        operator.set_password(session, app.state.network, "another long password", clock.now())

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
@pytest.mark.parametrize("path", PAGES)
def test_pages_render_with_an_empty_database(client, clock, path):
    log_in(client, clock)

    response = client.get(path)

    assert response.status_code == 200
    assert "<script" not in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


API_TOKEN_NAME = "agent"
CREDENTIAL = "instagram-credential-not-real"


@pytest.fixture
def sample(app, settings, clock, enrolled):
    """One of everything the pages show, plus every secret they must never show."""
    key = crypto.load_key(settings.secret_key_file)
    now = clock.now()
    network = app.state.network
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    (settings.media_dir / "photo.jpeg").write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)
    with app.state.sessionmaker() as session, session.begin():
        _, api_secret = create_token(session, API_TOKEN_NAME, {"posts": "read"})
        session.add(
            Connection(
                network=network,
                external_account_id="17840000000000001",
                username="sample_account",
                account_type="BUSINESS",
                credential_encrypted=seal(key, network, CREDENTIAL),
                credential_expires_at=now + timedelta(days=40),
                limits={"quota_usage": 3, "quota_total": 100, "quota_duration_seconds": 86400},
                checked_at=now,
            )
        )
        photo = Media(relative_path="photo.jpeg", sha256="a" * 64, type="jpeg", bytes=68)
        session.add(photo)
        session.flush()
        published = Post(
            network=network,
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
            network=network,
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
                    network=network,
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
                    network=network,
                    media_ref="18000000000000001",
                    taken_at=now - timedelta(hours=hours),
                    product_type="FEED",
                    published_at=now - timedelta(days=2),
                    permalink="https://www.instagram.com/p/sample/",
                    metrics={"reach": 100 + hours, "likes": 7},
                )
            )
        rule = Rule(
            network=network,
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
            network=network,
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
                network=network,
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
    return [
        *PAGES,
        f"/ui/posts/{sample['post_id']}",
        "/ui/stats/18000000000000001",
        f"/ui/media/{sample['media_id']}",
    ]


def test_pages_render_sample_data_without_writing_or_showing_secrets(app, client, clock, sample):
    log_in(client, clock)
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
    assert "@sample_account" in bodies["/ui/"]
    assert "Next one" in bodies["/ui/"]
    assert "A caption written by the operator" in bodies["/ui/posts"]
    assert f'src="/ui/media/{sample["media_id"]}"' in bodies["/ui/posts"]
    assert '<svg class="chart chart--primary"' in bodies["/ui/stats"]
    assert "Price question" in bodies["/ui/events"]
    assert "Here is the price list" in bodies["/ui/rules"]
    assert "not a published Meta rule" in bodies["/ui/rules"]  # one public variant: a warning
    assert "credential_expiring" in bodies["/ui/incidents"]


def test_the_rules_page_shows_a_rule_waiting_for_its_post(app, client, clock, enrolled):
    with app.state.sessionmaker() as session, session.begin():
        post = Post(network=app.state.network, type="image", content_hash="hash")
        session.add(post)
        session.flush()
        post_id = post.id
        session.add(
            Rule(
                network=app.state.network,
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

    page = client.get("/ui/rules").text

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
        for status, caption in (("draft", "A draft in progress"), ("cancelled", "Called off")):
            session.add(
                Post(
                    network=app.state.network,
                    type="image",
                    status=status,
                    caption=caption,
                    content_hash="d" * 64,
                    items=[PostItem(position=0, media_id=sample["media_id"])],
                )
            )
    log_in(client, clock)

    page = client.get("/ui/posts").text

    published, folded = page.index("A caption written by the operator"), page.index("<details")
    assert published < folded < page.index("A draft in progress") < page.index("Called off")


@pytest.mark.usefixtures("enrolled")
def test_missing_post_and_media_are_not_found(client, clock):
    log_in(client, clock)

    assert client.get("/ui/posts/nope").status_code == 404
    assert client.get("/ui/stats/nope").status_code == 404
    assert client.get("/ui/media/nope").status_code == 404


def test_deleted_media_file_shows_no_thumbnail(app, client, clock, sample, settings):
    (settings.media_dir / "photo.jpeg").unlink()
    log_in(client, clock)

    assert "/ui/media/" not in client.get("/ui/posts").text
    assert client.get(f"/ui/media/{sample['media_id']}").status_code == 404


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
        operator.enroll_totp(
            session, app.state.network, key, TOTP_SECRET, "１２３４５６", clock.now()
        )


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
        for hours, caption in ((2, "Due soonest"), (48, "Due last")):
            session.add(
                Post(
                    network=app.state.network,
                    type="image",
                    status="scheduled",
                    caption=caption,
                    content_hash="e" * 64,
                    scheduled_for=clock.now() + timedelta(hours=hours),
                    items=[PostItem(position=0, media_id=sample["media_id"])],
                )
            )

    with app.state.read_sessionmaker() as session:
        found = next_scheduled(session, app.state.network, limit=2)

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
    assert incident.network == app.state.network
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
        operator.enroll_totp(
            session, app.state.network, key, TOTP_SECRET, code_at(clock.now()), clock.now()
        )

    assert password_incidents(app)[0].closed_at is not None


@pytest.mark.usefixtures("enrolled")
def test_wrong_codes_from_before_a_password_change_do_not_reopen_the_incident(app, client, clock):
    fail_the_code(client, clock, times=3)
    clock.advance(minutes=1)
    new_password = "another long password"
    with app.state.sessionmaker() as session, session.begin():
        operator.set_password(session, app.state.network, new_password, clock.now())
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
