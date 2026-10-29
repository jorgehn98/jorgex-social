import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from jorgex_social.access.tokens import create_token
from jorgex_social.account import crypto
from jorgex_social.account.connection import ConnectError, ConnectionTask, connect
from jorgex_social.account.credential import NotConnected, StoredCredential, credential_context
from jorgex_social.account.models import AuditEntry, ChannelIncident, Connection
from jorgex_social.api import create_app
from jorgex_social.clock import ManualClock
from jorgex_social.config import Settings
from jorgex_social.instagram import make_instagram
from jorgex_social.instagram.client import InstagramClient
from jorgex_social.instagram.errors import AuthError, Unavailable
from jorgex_social.instagram.fake import FakeInstagram

START = datetime(2026, 10, 29, 12, tzinfo=UTC)
PASTED = "pasted-token-not-real"
RENEWED = "renewed-token-not-real"
SIXTY_DAYS = 60 * 86400


@pytest.fixture
def clock():
    return ManualClock(START)


@pytest.fixture
def ig(clock):
    return FakeInstagram(clock)


@pytest.fixture
def app(settings, clock):
    return create_app(settings, clock)


@pytest.fixture
def sessions(app):
    return app.state.sessionmaker


@pytest.fixture
def key(settings):
    return crypto.load_key(settings.secret_key_file)


@pytest.fixture
def task(ig, clock, settings):
    return ConnectionTask(ig, clock, settings.secret_key_file)


def headers_for(app, **grants):
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(session, f"agent-{len(grants)}-{sorted(grants)}", grants)
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def headers(app):
    return headers_for(app, account="write", posts="write", scheduling="write")


class StubTransport:
    """Answers by URL, like Meta would, and keeps what was sent."""

    def __init__(self, renewed=RENEWED):
        self.renewed = renewed
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers))
        if "refresh_access_token" in url:
            payload = {"access_token": self.renewed, "expires_in": SIXTY_DAYS}
        elif "content_publishing_limit" in url:
            payload = {"data": [{"quota_usage": 1, "config": {"quota_total": 100}}]}
        else:
            payload = {"user_id": "1784", "username": "test_account", "account_type": "BUSINESS"}
        return 200, json.dumps(payload).encode()


def count_calls(ig, operation):
    calls = []
    original = getattr(ig, operation)

    def spy(*args):
        calls.append(args)
        return original(*args)

    setattr(ig, operation, spy)
    return calls


def connection(sessions, network="simulation"):
    with sessions() as session:
        return session.scalar(select(Connection).where(Connection.network == network))


def open_incidents(sessions):
    with sessions() as session:
        return sorted(
            session.scalars(
                select(ChannelIncident.type).where(ChannelIncident.closed_at.is_(None))
            ).all()
        )


def incident_count(sessions, type_):
    with sessions() as session:
        return len(
            session.scalars(select(ChannelIncident).where(ChannelIncident.type == type_)).all()
        )


# The credential never shows


@pytest.fixture
def real(settings, clock):
    """An app in real mode: it never builds an Instagram client, so it needs no network."""
    real_settings = Settings(
        database_path=settings.database_path,
        media_dir=settings.media_dir,
        secret_key_file=settings.secret_key_file,
        simulation=False,
    )
    return create_app(real_settings, clock)


def test_credential_is_stored_only_encrypted_and_never_shown(
    real, settings, key, clock, caplog, capsys
):
    from fastapi.testclient import TestClient

    caplog.set_level(logging.DEBUG)
    sessions = real.state.sessionmaker
    transport = StubTransport()
    instagram = InstagramClient(PASTED, "me", transport=transport)

    stored = connect(sessions, instagram, PASTED, key, clock.now())

    client = TestClient(real)
    headers = headers_for(real, account="write")
    client.put("/v1/account/kill-switch", json={"enabled": False}, headers=headers)
    shown = [
        client.get("/v1/account", headers=headers).text,
        client.get("/v1/account/audit", headers=headers).text,
        client.get("/v1/account/incidents", headers=headers).text,
        repr(stored),
        repr(instagram),
        caplog.text,
        capsys.readouterr().out,
    ]
    on_disk = b"".join(
        path.read_bytes() for path in settings.database_path.parent.glob("jorgex_social.db*")
    )

    for secret in (PASTED, RENEWED):
        assert all(secret not in text for text in shown)
        assert secret.encode() not in on_disk
    assert json.loads(shown[0])["account"]["username"] == "test_account"
    assert crypto.decrypt(key, stored.credential_encrypted, credential_context("instagram")) == (
        RENEWED
    )


def test_ciphertext_is_bound_to_its_network(key):
    blob = crypto.encrypt(key, RENEWED, credential_context("instagram"))

    with pytest.raises(crypto.SecretKeyError):
        crypto.decrypt(key, blob, credential_context("other"))


def test_worker_client_uses_the_renewed_token_at_once(real, key, clock, settings):
    sessions = real.state.sessionmaker
    connect(
        sessions, InstagramClient(PASTED, "me", transport=StubTransport()), PASTED, key, clock.now()
    )
    transport = StubTransport(renewed="second-renewal-not-real")
    worker_client = InstagramClient(
        StoredCredential(sessions, key, "instagram"), "1784", transport=transport
    )
    task = ConnectionTask(worker_client, clock, settings.secret_key_file)

    clock.advance(days=11)
    task(sessions)

    refresh, account, _limit = transport.calls
    assert f"access_token={RENEWED}" in refresh[1]
    assert account[2]["Authorization"] == "Bearer second-renewal-not-real"


def test_real_mode_refuses_to_start_without_the_key(settings, tmp_path, clock):
    no_key = Settings(
        database_path=settings.database_path,
        secret_key_file=tmp_path / "absent.key",
        simulation=False,
    )

    with pytest.raises(crypto.SecretKeyError, match="secret key file .* is missing"):
        make_instagram(no_key, clock)


def test_real_mode_refuses_to_start_without_a_connection(settings, clock):
    with pytest.raises(NotConnected, match="connect-instagram"):
        make_instagram(
            Settings(
                database_path=settings.database_path,
                secret_key_file=settings.secret_key_file,
                simulation=False,
            ),
            clock,
        )


def test_a_token_too_young_to_renew_is_stored_with_an_estimated_expiry(
    sessions, key, clock, settings, caplog
):
    young = FakeInstagram(clock, credential_issued_at=clock.now())  # generated today
    refreshes = count_calls(young, "refresh_token")

    stored = connect(sessions, young, PASTED, key, clock.now())

    assert stored.credential_expires_at == clock.now() + timedelta(days=59)
    assert stored.credential_refreshed_at == clock.now()
    assert crypto.decrypt(key, stored.credential_encrypted, credential_context("simulation")) == (
        PASTED
    )
    assert "expiry is estimated" in caplog.text
    assert PASTED not in caplog.text

    # The worker renews it once Meta accepts, well before the estimate runs out.
    task = ConnectionTask(young, clock, settings.secret_key_file)
    clock.advance(days=10)
    task(sessions)
    assert len(refreshes) == 2  # the refused one at connect time, then the real renewal
    assert connection(sessions).credential_expires_at == clock.now() + timedelta(days=60)


def test_an_invalid_token_is_not_stored(sessions, key, clock):
    revoked = FakeInstagram(clock)
    revoked.fail_next("refresh_token", AuthError("Instagram API 400: invalid token", code=190))

    with pytest.raises(AuthError):
        connect(sessions, revoked, PASTED, key, clock.now())

    assert connection(sessions) is None


def other_account(clock):
    other = FakeInstagram(clock)
    real_account = other.account
    other.account = lambda: replace(real_account(), id="999", username="other_account")
    return other


def test_another_account_needs_replace(sessions, ig, key, clock, client, headers):
    first = connect(sessions, ig, PASTED, key, clock.now())

    with pytest.raises(ConnectError, match="--replace"):
        connect(sessions, other_account(clock), "other-token-not-real", key, clock.now())
    assert connection(sessions).external_account_id == first.external_account_id

    connect(sessions, other_account(clock), "other-token-not-real", key, clock.now(), replace=True)

    assert (connection(sessions).external_account_id, connection(sessions).username) == (
        "999",
        "other_account",
    )
    with sessions() as session:
        actions = session.scalars(
            select(AuditEntry.action).where(AuditEntry.actor_type == "operator")
        ).all()
    assert sorted(actions) == ["connection.connected", "connection.replaced"]


# Renewal


def test_renews_about_ten_days_after_the_last_renewal(sessions, ig, key, clock, task):
    connect(sessions, ig, PASTED, key, clock.now())
    refreshes = count_calls(ig, "refresh_token")

    clock.advance(days=9, hours=23)
    task(sessions)
    assert refreshes == []

    clock.advance(hours=2)  # 10 days and 1 hour: 49 days and 23 hours left
    task(sessions)

    assert len(refreshes) == 1
    assert connection(sessions).credential_expires_at == clock.now() + timedelta(days=60)
    with sessions() as session:
        actions = session.scalars(select(AuditEntry.action)).all()
    assert "credential.renewed" in actions


def test_never_renews_a_credential_younger_than_24_hours(sessions, clock, key, settings):
    # Meta grants less than 50 days: the threshold alone would renew on every check.
    short = FakeInstagram(clock, credential_lifetime=timedelta(days=40))
    connect(sessions, short, PASTED, key, clock.now())
    refreshes = count_calls(short, "refresh_token")
    task = ConnectionTask(short, clock, settings.secret_key_file)

    for _ in range(23):
        clock.advance(hours=1)
        task(sessions)
    assert refreshes == []

    clock.advance(hours=2)
    task(sessions)
    assert len(refreshes) == 1


def test_never_renews_an_expired_credential(sessions, ig, key, clock, task, client, headers):
    connect(sessions, ig, PASTED, key, clock.now())
    refreshes = count_calls(ig, "refresh_token")

    clock.advance(days=61)  # the worker was down all along
    task(sessions)

    assert refreshes == []
    assert connection(sessions).status == "expired"
    assert open_incidents(sessions) == ["credential_expired"]
    assert client.get("/v1/account", headers=headers).json()["status"] == "expired"


def test_renewal_needs_the_key_before_asking_meta(sessions, ig, key, clock, task, settings):
    connect(sessions, ig, PASTED, key, clock.now())
    refreshes = count_calls(ig, "refresh_token")
    settings.secret_key_file.unlink()

    clock.advance(days=11)
    with pytest.raises(crypto.SecretKeyError):
        task(sessions)

    assert refreshes == []  # a new token that could not be stored would be lost


# Status and incidents


def test_expiring_incident_opens_once_and_closes_after_renewal(sessions, ig, key, clock, task):
    connect(sessions, ig, PASTED, key, clock.now())
    clock.advance(days=51)  # nine days left: renewals have failed for weeks

    for _ in range(3):
        ig.fail_next("refresh_token", Unavailable("503"))
        task(sessions)
        clock.advance(hours=1)

    assert connection(sessions).status == "expiring"
    assert incident_count(sessions, "credential_expiring") == 1

    task(sessions)  # renewal works again

    assert connection(sessions).status == "ok"
    assert open_incidents(sessions) == []


def test_revoked_credential_opens_an_incident_until_it_works_again(sessions, ig, key, clock, task):
    connect(sessions, ig, PASTED, key, clock.now())
    ig.revoke()

    task(sessions)
    clock.advance(hours=1)
    task(sessions)

    assert connection(sessions).status == "revoked"
    assert open_incidents(sessions) == ["permission_revoked"]
    assert incident_count(sessions, "permission_revoked") == 1

    ig.revoked = False  # reconnected or permission granted again
    clock.advance(hours=1)
    task(sessions)

    assert connection(sessions).status == "ok"
    assert open_incidents(sessions) == []


def test_persistent_api_error_after_three_failed_checks(sessions, ig, clock, task):
    for _ in range(2):
        ig.fail_next("account", Unavailable("503"))
        task(sessions)
        clock.advance(hours=1)
    assert open_incidents(sessions) == []

    for _ in range(2):
        ig.fail_next("account", Unavailable("503"))
        task(sessions)
        clock.advance(hours=1)
    assert open_incidents(sessions) == ["persistent_api_error"]
    assert incident_count(sessions, "persistent_api_error") == 1

    task(sessions)
    assert open_incidents(sessions) == []


def test_checks_at_most_once_an_hour(sessions, ig, clock, task):
    checks = count_calls(ig, "account")

    task(sessions)
    clock.advance(minutes=59)
    task(sessions)
    clock.advance(minutes=2)
    task(sessions)

    assert len(checks) == 2


def test_one_open_incident_per_cause_is_enforced_by_the_database(sessions, clock):
    def add():
        with sessions() as session, session.begin():
            session.add(
                ChannelIncident(network="simulation", type="credential_expired", opened_at=START)
            )

    add()
    with pytest.raises(IntegrityError):
        add()


# API


def test_simulation_answers_before_and_after_the_first_check(client, headers, sessions, task):
    before = client.get("/v1/account", headers=headers).json()

    task(sessions)
    after = client.get("/v1/account", headers=headers).json()

    assert (before["network"], before["status"], before["account"]) == ("simulation", "ok", None)
    assert after["status"] == "ok"
    assert after["account"]["username"] == "simulated_account"
    assert after["limits"]["quota_total"] == 100
    assert after["credential_expires_at"] is None
    assert (after["kill_switch"], after["open_incidents"]) == (False, 0)


def test_real_mode_without_a_connection_is_missing(real):
    from fastapi.testclient import TestClient

    response = TestClient(real).get("/v1/account", headers=headers_for(real, account="read"))

    assert response.json()["status"] == "missing"


def test_acknowledging_does_not_close(client, headers, sessions, ig, task):
    ig.revoke()
    task(sessions)
    [incident] = client.get("/v1/account/incidents?open=true", headers=headers).json()

    response = client.post(f"/v1/account/incidents/{incident['id']}/acknowledge", headers=headers)

    assert response.status_code == 200
    assert response.json()["acknowledged_at"] is not None
    assert response.json()["closed_at"] is None
    assert client.get("/v1/account", headers=headers).json()["open_incidents"] == 1
    assert client.post("/v1/account/incidents/nope/acknowledge", headers=headers).status_code == 404


def test_incident_filter(client, headers, sessions, ig, clock, task):
    ig.revoke()
    task(sessions)
    ig.revoked = False
    clock.advance(hours=1)
    task(sessions)

    closed = client.get("/v1/account/incidents?open=false", headers=headers).json()

    assert [i["type"] for i in closed] == ["permission_revoked"]
    assert client.get("/v1/account/incidents?open=true", headers=headers).json() == []


def test_account_writes_need_account_write(app, client):
    reader = headers_for(app, account="read")

    assert client.get("/v1/account", headers=reader).status_code == 200
    assert (
        client.put("/v1/account/kill-switch", json={"enabled": True}, headers=reader).status_code
        == 403
    )


def test_kill_switch_round_trip(client, headers):
    on = client.put("/v1/account/kill-switch", json={"enabled": True}, headers=headers)

    assert on.status_code == 200
    assert on.json()["enabled"] is True
    assert client.get("/v1/account", headers=headers).json()["kill_switch"] is True


# Audit


def audit(client, headers, **params):
    return client.get("/v1/account/audit", params=params, headers=headers).json()


def test_write_requests_are_audited_without_values(app, client, headers):
    media = client.post(
        "/v1/media",
        content=b"\xff\xd8\xff" + b"j" * 32,
        headers={**headers, "Content-Type": "image/jpeg"},
    ).json()
    created = client.post(
        "/v1/posts",
        json={"type": "image", "caption": "a caption only we wrote", "items": [media["id"]]},
        headers=headers,
    ).json()
    client.post(f"/v1/posts/{created['id']}/cancel", headers=headers)
    client.put("/v1/account/kill-switch", json={"enabled": True}, headers=headers)
    client.get("/v1/posts", headers=headers)

    entries = audit(client, headers)

    assert [(e["action"], e["outcome"]) for e in entries] == [
        ("PUT /v1/account/kill-switch", "ok"),
        ("POST /v1/posts/{post_id}/cancel", "ok"),
        ("POST /v1/posts", "ok"),
        ("POST /v1/media", "ok"),
    ]
    prefix = headers["Authorization"].removeprefix("Bearer ")[:12]
    assert {(e["actor_type"], e["actor_ref"]) for e in entries} == {("token", prefix)}
    assert (entries[1]["object_type"], entries[1]["object_id"]) == ("post", created["id"])
    assert entries[0]["detail"] == {"enabled": True}
    assert "caption" not in json.dumps(entries)
    assert audit(client, headers, object_type="post", object_id=created["id"]) == [entries[1]]


def test_denied_and_failed_requests_are_audited(app, client, headers):
    reader = headers_for(app, account="read")

    client.put("/v1/account/kill-switch", json={"enabled": True}, headers=reader)
    client.put("/v1/account/kill-switch", json={"enabled": True})
    client.post("/v1/account/incidents/missing/acknowledge", headers=headers)

    entries = audit(client, headers)
    assert [(e["outcome"], e["actor_ref"] is not None) for e in entries] == [
        ("error", True),  # 404
        ("denied", False),  # no token
        ("denied", True),  # a token without account:write
    ]
    assert entries[0]["object_id"] == "missing"


def test_unknown_routes_are_not_audited(client, headers):
    client.post("/v1/nothing-here/secret-looking-value", headers=headers)

    assert audit(client, headers) == []


def test_audit_log_is_append_only(client, headers, sessions):
    client.put("/v1/account/kill-switch", json={"enabled": True}, headers=headers)

    for statement in ("UPDATE audit_log SET outcome = 'ok'", "DELETE FROM audit_log"):
        with pytest.raises(Exception, match="append-only"), sessions() as session:
            session.execute(text(statement))
