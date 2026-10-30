from datetime import timedelta

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from jorgex_social.access import deps
from jorgex_social.access.deps import require
from jorgex_social.access.models import ApiToken
from jorgex_social.access.tokens import Group, Level, TokenError, create_token
from jorgex_social.api import create_app
from jorgex_social.clock import ManualClock
from jorgex_social.db import utcnow


def make_token(app, name="agent", **grants):
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(session, name, grants)
    return secret


def auth(secret):
    return {"Authorization": f"Bearer {secret}"}


def test_secret_is_not_stored(app):
    secret = make_token(app, posts="write")

    with app.state.sessionmaker() as session:
        token = session.scalar(select(ApiToken))
    assert secret not in (token.hash, token.prefix)
    assert secret.startswith(token.prefix)


def test_ungranted_groups_are_denied(app, client):
    secret = make_token(app, stats="read")

    body = client.get("/v1/token", headers=auth(secret)).json()

    assert body["permissions"] == {
        "posts": "deny",
        "scheduling": "deny",
        "rules": "deny",
        "events": "deny",
        "stats": "read",
        "account": "deny",
    }


def test_token_use_is_recorded(app, client):
    secret = make_token(app)

    client.get("/v1/token", headers=auth(secret))

    with app.state.sessionmaker() as session:
        assert session.scalar(select(ApiToken.last_used_at)) is not None


@pytest.mark.parametrize("headers", [{}, auth("jxs_unknown"), {"Authorization": "Basic x"}])
def test_rejects_missing_or_unknown_token(client, headers):
    response = client.get("/v1/token", headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_token"


def test_rejects_revoked_token(app, client):
    secret = make_token(app)
    with app.state.sessionmaker() as session, session.begin():
        session.scalar(select(ApiToken)).revoked_at = utcnow()

    assert client.get("/v1/token", headers=auth(secret)).status_code == 401


@pytest.mark.parametrize(
    ("granted", "required", "allowed"),
    [
        ("write", Level.WRITE, True),
        ("write", Level.READ, True),
        ("read", Level.READ, True),
        ("read", Level.WRITE, False),
        ("deny", Level.READ, False),
    ],
)
def test_group_permissions(app, client, granted, required, allowed):
    @app.get("/probe", dependencies=[Depends(require(Group.POSTS, required))])
    def probe():
        return {"ok": True}

    secret = make_token(app, posts=granted)

    response = client.get("/probe", headers=auth(secret))

    assert response.status_code == (200 if allowed else 403)


@pytest.mark.parametrize("grants", [{"unknown": "read"}, {"posts": "admin"}])
def test_rejects_unknown_group_or_level(app, grants):
    with pytest.raises(TokenError), app.state.sessionmaker() as session:
        create_token(session, "agent", grants)


def test_rejects_duplicate_name(app):
    make_token(app, "agent")

    with pytest.raises(TokenError), app.state.sessionmaker() as session:
        create_token(session, "agent", {})


def test_unknown_token_is_rejected_while_the_database_is_being_written(app, client):
    """Authentication reads in a deferred transaction, so made-up tokens never queue on the write
    lock (and a flood of them cannot stall the worker)."""
    with app.state.sessionmaker() as writer, writer.begin():
        writer.execute(select(ApiToken.id)).all()
        response = client.get("/v1/token", headers=auth("jxs_unknown"))

    assert response.status_code == 401


def test_token_use_is_recorded_at_most_once_a_minute(settings):
    clock = ManualClock()
    app = create_app(settings, clock)
    client = TestClient(app)
    secret = make_token(app)

    def last_used():
        with app.state.read_sessionmaker() as session:
            return session.scalar(select(ApiToken.last_used_at))

    client.get("/v1/token", headers=auth(secret))
    first = last_used()
    clock.advance(seconds=30)
    # Used recently: no write, so it answers even while another connection holds the write lock.
    with app.state.sessionmaker() as writer, writer.begin():
        writer.execute(select(ApiToken.id)).all()
        assert client.get("/v1/token", headers=auth(secret)).status_code == 200
    throttled = last_used()
    clock.advance(seconds=31)
    client.get("/v1/token", headers=auth(secret))

    assert first == clock.now() - timedelta(seconds=61)
    assert throttled == first
    assert last_used() == clock.now()


def test_token_use_bookkeeping_never_fails_the_request(app, client, monkeypatch, caplog):
    def locked(*_):
        raise OperationalError("UPDATE api_tokens", {}, Exception("database is locked"))

    monkeypatch.setattr(deps, "record_use", locked)
    secret = make_token(app)

    response = client.get("/v1/token", headers=auth(secret))

    assert response.status_code == 200
    assert "last use" in caplog.text
