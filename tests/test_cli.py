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
    monkeypatch.setenv("JORGEX_SOCIAL_DATABASE_PATH", str(path))

    main(["migrate"])

    from jorgex_social.config import Settings

    tables = inspect(make_engine(Settings(database_path=path))).get_table_names()
    assert "api_tokens" in tables


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
