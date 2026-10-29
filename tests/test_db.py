from fastapi.testclient import TestClient
from sqlalchemy import text

from jorgex_social.access.deps import SessionDep
from jorgex_social.access.models import ApiToken
from jorgex_social.db import make_engine, make_sessionmaker


def test_reads_happen_inside_the_transaction(settings):
    """A read-then-write must not interleave with the other process (BEGIN IMMEDIATE)."""
    sessions = make_sessionmaker(make_engine(settings))

    with sessions() as session, session.begin():
        session.execute(text("SELECT 1"))
        assert session.connection().connection.dbapi_connection.in_transaction


def test_failed_commit_is_an_error_not_a_success(app):
    """The commit runs before the response, so the client never sees 2xx for lost data."""

    @app.post("/probe")
    def probe(session: SessionDep):
        for _ in range(2):  # same unique name: only fails when the commit flushes
            session.add(ApiToken(name="dup", prefix="p", hash=str(_), permissions={}))
        return {"ok": True}

    response = TestClient(app, raise_server_exceptions=False).post("/probe")

    assert response.status_code == 500


def test_foreign_keys_are_enforced(settings):
    with make_engine(settings).connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
