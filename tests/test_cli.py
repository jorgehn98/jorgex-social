import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, inspect, select

from jorgex_social.access.models import ApiToken
from jorgex_social.access.tokens import create_token
from jorgex_social.account import scope
from jorgex_social.account.credential import seal
from jorgex_social.account.crypto import load_key
from jorgex_social.account.models import AuditEntry, Connection
from jorgex_social.cli import main, worker_tasks
from jorgex_social.clock import ManualClock
from jorgex_social.config import Settings
from jorgex_social.db import make_engine, make_sessionmaker
from jorgex_social.instagram import network_for
from jorgex_social.instagram.fake import FakeInstagram
from jorgex_social.publishing.models import Post
from jorgex_social.replies.task import ReplyTask

START = datetime(2026, 10, 29, 12, tzinfo=UTC)


def connect_account(settings, monkeypatch) -> None:
    """Connects the simulated account, as an install does before creating its first token."""
    import io

    monkeypatch.setenv("JORGEX_SOCIAL_SECRET_KEY_FILE", str(settings.secret_key_file))
    monkeypatch.setattr("sys.stdin", io.StringIO("pasted-token-not-real\n"))
    main(["connect-instagram"])


def connect_accounts(settings, monkeypatch, *account_ids) -> None:
    """Runs `connect-instagram` once per id: each command gets a fake Instagram that answers for
    that account, as a pasted token would."""
    import io

    ids = iter(account_ids)

    class AccountedFake(FakeInstagram):
        def __init__(self, clock):
            super().__init__(clock, account_id=next(ids))

    monkeypatch.setattr("jorgex_social.cli.FakeInstagram", AccountedFake)
    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(settings.database_path))
    monkeypatch.setenv("JORGEX_SOCIAL_SECRET_KEY_FILE", str(settings.secret_key_file))
    for account_id in account_ids:
        monkeypatch.setattr("sys.stdin", io.StringIO(f"pasted-token-{account_id}\n"))
        main(["connect-instagram"])


def test_create_token_prints_secret_once(settings, monkeypatch, capsys):
    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(settings.database_path))
    connect_account(settings, monkeypatch)
    capsys.readouterr()

    main(
        [
            "create-token",
            "--name",
            "agent",
            "--account",
            "simulated_account",
            "--grant",
            "posts=write",
        ]
    )

    out = json.loads(capsys.readouterr().out)
    assert out["token"].startswith("jxs_")
    assert out["permissions"]["posts"] == "write"
    assert out["account"]["username"] == "simulated_account"
    assert out["account"]["network"] == "simulation"


def test_create_token_needs_a_connected_account(settings, monkeypatch, capsys):
    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(settings.database_path))

    with pytest.raises(SystemExit, match="no account is connected"):
        main(["create-token", "--name", "agent", "--account", "simulated_account"])

    connect_account(settings, monkeypatch)
    capsys.readouterr()

    with pytest.raises(SystemExit, match="no connected account is named"):
        main(["create-token", "--name", "agent", "--account", "somebody_else"])


def test_create_token_accepts_the_account_id(settings, monkeypatch, capsys):
    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(settings.database_path))
    connect_account(settings, monkeypatch)
    capsys.readouterr()

    main(["create-token", "--name", "agent", "--account", "simulated_account"])
    account_id = json.loads(capsys.readouterr().out)["account"]["connection_id"]
    main(["create-token", "--name", "reader", "--account", account_id])

    listed = json.loads(capsys.readouterr().out)
    assert listed["account"]["connection_id"] == account_id
    main(["list-tokens"])
    tokens = {token["name"]: token for token in json.loads(capsys.readouterr().out)}
    assert tokens["reader"]["account"] == "simulated_account"
    assert tokens["reader"]["account_id"] == account_id


def test_migrate_creates_schema_from_scratch(tmp_path, monkeypatch):
    path = tmp_path / "fresh" / "jorgex_social.db"
    key = tmp_path / "keys" / "secret.key"
    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(path))
    monkeypatch.setenv("JORGEX_SOCIAL_SECRET_KEY_FILE", str(key))

    main(["migrate"])

    from jorgex_social.config import Settings

    tables = inspect(make_engine(Settings(database_path=path))).get_table_names()
    assert "api_tokens" in tables
    assert len(key.read_bytes()) == 32
    assert key.stat().st_mode & 0o777 == 0o600

    before = key.read_bytes()
    main(["migrate"])
    assert key.read_bytes() == before  # never replaced: it would orphan the stored credential


def test_worker_stops_on_sigterm(settings):
    import os
    import select
    import signal
    import subprocess
    import sys

    env = {**os.environ, "JORGEX_SOCIAL_DATABASE_PATH": str(settings.database_path)}
    proc = subprocess.Popen(
        [sys.executable, "-c", "from jorgex_social.cli import main; main(['worker'])"],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        assert ready and "worker started" in proc.stdout.readline(), "worker did not start"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
    finally:
        proc.kill()


def test_revoked_token_is_listed_without_secret(settings, monkeypatch, capsys):
    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(settings.database_path))
    connect_account(settings, monkeypatch)
    capsys.readouterr()
    main(["create-token", "--name", "agent", "--account", "simulated_account"])
    secret = json.loads(capsys.readouterr().out)["token"]

    main(["revoke-token", "--name", "agent"])
    capsys.readouterr()
    main(["list-tokens"])
    listing = capsys.readouterr().out

    assert secret not in listing
    assert json.loads(listing)[0]["revoked_at"] is not None


def test_a_second_worker_on_the_same_database_refuses_to_start(settings, monkeypatch):
    from jorgex_social.cli import acquire_worker_lock

    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(settings.database_path))
    first = acquire_worker_lock(settings)
    try:
        with pytest.raises(SystemExit, match="another worker"):
            main(["worker"])
    finally:
        first.close()

    acquire_worker_lock(settings).close()  # free again once the first one is gone


def env_for(monkeypatch, settings, **extra):
    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(settings.database_path))
    monkeypatch.setenv("JORGEX_SOCIAL_SECRET_KEY_FILE", str(settings.secret_key_file))
    for name, value in extra.items():
        monkeypatch.setenv(f"JORGEX_SOCIAL_{name}", value)


def test_connect_instagram_reads_the_token_from_stdin(settings, monkeypatch, capsys):
    import io

    from sqlalchemy import select

    from jorgex_social.account.models import Connection
    from jorgex_social.db import make_sessionmaker

    env_for(monkeypatch, settings)
    monkeypatch.setattr("sys.stdin", io.StringIO("pasted-token-not-real\n"))

    main(["connect-instagram"])

    out = capsys.readouterr()
    shown = json.loads(out.out)
    assert (shown["network"], shown["username"]) == ("simulation", "simulated_account")
    assert shown["result"] == "added"
    assert shown["credential_expires_at"]
    assert "pasted-token-not-real" not in out.out + out.err
    with make_sessionmaker(make_engine(settings))() as session:
        assert session.scalar(select(Connection)).credential_encrypted


def test_connect_instagram_needs_the_key(settings, monkeypatch):
    import io

    env_for(monkeypatch, settings)
    settings.secret_key_file.unlink()
    monkeypatch.setattr("sys.stdin", io.StringIO("pasted-token-not-real\n"))

    with pytest.raises(SystemExit, match="secret key file .* is missing"):
        main(["connect-instagram"])


def test_connect_instagram_refreshes_the_same_account(settings, monkeypatch, capsys):
    """Connecting the same account again renews its credential in place: no second row, and the
    posts and tokens it already owns stay where they are."""
    env_for(monkeypatch, settings)
    connect_accounts(settings, monkeypatch, "1784")
    capsys.readouterr()
    sessions = make_sessionmaker(make_engine(settings))
    with sessions() as session, session.begin():
        first = session.scalar(select(Connection))
        session.add(Post(account_id=first.id, type="image", content_hash="h"))
        create_token(session, "agent", {"posts": "read"}, first.id)
        first_id, first_credential = first.id, first.credential_encrypted

    connect_accounts(settings, monkeypatch, "1784")

    refreshed = json.loads(capsys.readouterr().out)
    assert refreshed["result"] == "refreshed"
    assert refreshed["credential_expires_at"]
    with sessions() as session:
        rows = session.scalars(select(Connection)).all()
        assert [row.id for row in rows] == [first_id]
        assert rows[0].credential_encrypted != first_credential  # renewed and re-sealed
        assert session.scalar(select(func.count()).select_from(Post)) == 1
        assert session.scalar(select(func.count()).select_from(ApiToken)) == 1
        actions = session.scalars(select(AuditEntry.action).order_by(AuditEntry.occurred_at)).all()
    assert actions == ["connection.added", "connection.refreshed"]


def test_connect_instagram_adds_a_second_account_and_keeps_the_first(settings, monkeypatch, capsys):
    """The second own account is a second row with its own credential. The first account's row,
    credential, posts and tokens are untouched."""
    env_for(monkeypatch, settings)
    connect_accounts(settings, monkeypatch, "1784")
    capsys.readouterr()
    sessions = make_sessionmaker(make_engine(settings))
    with sessions() as session, session.begin():
        first = session.scalar(select(Connection))
        session.add(Post(account_id=first.id, type="image", content_hash="h"))
        create_token(session, "agent", {"posts": "read"}, first.id)
        first_id, first_credential = first.id, first.credential_encrypted

    connect_accounts(settings, monkeypatch, "2048")

    added = json.loads(capsys.readouterr().out)
    assert added["result"] == "added"
    assert added["credential_expires_at"]
    with sessions() as session:
        rows = session.scalars(select(Connection).order_by(Connection.id)).all()
        assert [row.external_account_id for row in rows] == ["1784", "2048"]
        untouched = session.get_one(Connection, first_id)
        assert (untouched.external_account_id, untouched.username) == (
            "1784",
            "simulated_account",
        )
        assert untouched.credential_encrypted == first_credential
        assert (
            session.scalar(
                select(func.count()).select_from(Post).where(Post.account_id == first_id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(ApiToken).where(ApiToken.account_id == first_id)
            )
            == 1
        )
        actions = session.scalars(select(AuditEntry.action).order_by(AuditEntry.occurred_at)).all()
    assert actions == ["connection.added", "connection.added"]


def test_connect_instagram_fills_the_draft_row(settings, monkeypatch, capsys):
    """A write before connecting already owns the mode's first row: connecting names it instead
    of adding another one."""
    env_for(monkeypatch, settings)
    sessions = make_sessionmaker(make_engine(settings))
    with sessions() as session, session.begin():
        draft_id = scope.current_account(session, network_for(Settings.from_env())).id

    connect_accounts(settings, monkeypatch, "1784")

    filled = json.loads(capsys.readouterr().out)
    assert filled["result"] == "added"
    assert filled["credential_expires_at"]
    with sessions() as session:
        rows = session.scalars(select(Connection)).all()
        assert [row.id for row in rows] == [draft_id]
        assert rows[0].external_account_id == "1784"
        actions = session.scalars(select(AuditEntry.action)).all()
    assert actions == ["connection.added"]


def test_connect_instagram_refuses_the_removed_replace_flag(settings, monkeypatch, capsys):
    env_for(monkeypatch, settings)

    with pytest.raises(SystemExit) as exit:
        main(["connect-instagram", "--replace"])

    assert exit.value.code == 2
    message = capsys.readouterr().err
    assert "--replace was removed" in message
    assert "adds it as another account" in message
    with make_sessionmaker(make_engine(settings))() as session:
        assert session.scalar(select(func.count()).select_from(Connection)) == 0


def test_worker_in_real_mode_refuses_to_start_without_the_key(settings, monkeypatch):
    env_for(monkeypatch, settings, SIMULATION="false")
    settings.secret_key_file.unlink()

    with pytest.raises(SystemExit, match="secret key file .* is missing"):
        main(["worker"])


def test_the_worker_hands_the_opt_out_words_to_the_reply_task(settings, monkeypatch):
    """One set of tasks per account, built with the installation's settings."""
    seen = {}

    class Recording(ReplyTask):
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

    env_for(monkeypatch, settings, OPT_OUT_WORDS="halt,enough")
    monkeypatch.setattr("jorgex_social.cli.ReplyTask", Recording)
    loaded = Settings.from_env()
    sessions = make_sessionmaker(make_engine(loaded))
    with sessions() as session, session.begin():
        scope.current_account(session, network_for(loaded))

    _, accounts = worker_tasks(loaded, ManualClock(START), sessions, None)
    accounts(sessions)

    assert seen["opt_out_words"] == ("halt", "enough")


def test_the_worker_only_builds_tasks_for_connected_accounts_in_real_mode(settings, monkeypatch):
    """A row a first write created has no account and no credential: the worker would have nothing
    to call for it, so it waits until `connect-instagram` names it."""
    real = Settings(
        database_path=settings.database_path,
        media_dir=settings.media_dir,
        secret_key_file=settings.secret_key_file,
        simulation=False,
    )
    key = load_key(real.secret_key_file)
    sessions = make_sessionmaker(make_engine(real))
    with sessions() as session, session.begin():
        connected = Connection(network="instagram", external_account_id="1784", username="a")
        session.add(connected)
        session.flush()
        connected.credential_encrypted = seal(key, connected.id, "token-not-real")
        session.add(Connection(network="instagram", external_account_id="", username=""))
        connected_id = connected.id
    built = []

    def recording(*args, **kwargs):
        built.append(kwargs["account_id"])
        return lambda sessions: None

    for name in ("PublishTask", "SnapshotTask", "ConnectionTask", "ReplyTask"):
        monkeypatch.setattr(f"jorgex_social.cli.{name}", recording)

    _, accounts = worker_tasks(real, ManualClock(START), sessions, key)
    accounts(sessions)

    assert set(built) == {connected_id}  # the draft row is not worked on
