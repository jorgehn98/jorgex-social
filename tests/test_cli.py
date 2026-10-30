import json

import pytest
from sqlalchemy import inspect

from jorgex_social.cli import main
from jorgex_social.db import make_engine


def test_create_token_prints_secret_once(settings, monkeypatch, capsys):
    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(settings.database_path))

    main(["create-token", "--name", "agent", "--grant", "posts=write"])

    out = json.loads(capsys.readouterr().out)
    assert out["token"].startswith("jxs_")
    assert out["permissions"]["posts"] == "write"


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
    main(["create-token", "--name", "agent"])
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


def test_connect_instagram_accepts_replace(settings, monkeypatch, capsys):
    import io

    env_for(monkeypatch, settings)
    monkeypatch.setattr("sys.stdin", io.StringIO("pasted-token-not-real\n"))

    main(["connect-instagram", "--replace"])

    assert json.loads(capsys.readouterr().out)["username"] == "simulated_account"


def test_worker_in_real_mode_refuses_to_start_without_the_key(settings, monkeypatch):
    env_for(monkeypatch, settings, SIMULATION="false")
    settings.secret_key_file.unlink()

    with pytest.raises(SystemExit, match="secret key file .* is missing"):
        main(["worker"])


def test_the_worker_hands_the_opt_out_words_to_the_reply_task(settings, monkeypatch):
    class Built(Exception):
        pass

    seen = {}

    def reply_task(*args, **kwargs):
        seen.update(kwargs)
        raise Built

    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(settings.database_path))
    monkeypatch.setenv("JORGEX_SOCIAL_SECRET_KEY_FILE", str(settings.secret_key_file))
    monkeypatch.setenv("JORGEX_SOCIAL_OPT_OUT_WORDS", "halt,enough")
    monkeypatch.setattr("jorgex_social.cli.ReplyTask", reply_task)
    monkeypatch.setattr("jorgex_social.cli.logs.configure", lambda: None)

    with pytest.raises(Built):
        main(["worker"])

    assert seen["opt_out_words"] == ("halt", "enough")
