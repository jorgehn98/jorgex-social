import argparse
import fcntl
import getpass
import json
import logging
import signal
import sys
import threading
from datetime import timedelta
from importlib import resources

import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from jorgex_social import logs, worker
from jorgex_social.access.models import ApiToken
from jorgex_social.access.tokens import Group, Level, TokenError, create_token, revoke_token
from jorgex_social.account.connection import ConnectError, ConnectionTask, connect
from jorgex_social.account.credential import NotConnected
from jorgex_social.account.crypto import SecretKeyError, ensure_key, load_key
from jorgex_social.clock import SystemClock
from jorgex_social.config import Settings
from jorgex_social.db import make_engine, make_sessionmaker
from jorgex_social.instagram import make_instagram
from jorgex_social.instagram.client import InstagramClient
from jorgex_social.instagram.errors import InstagramError
from jorgex_social.instagram.fake import FakeInstagram
from jorgex_social.publishing.worker import SIMULATED_PUBLIC_URL, PublishTask
from jorgex_social.stats.snapshots import SnapshotTask

log = logging.getLogger(__name__)


def alembic_config(settings: Settings) -> Config:
    config = Config()
    config.set_main_option("script_location", str(resources.files("jorgex_social") / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def migrate(settings: Settings) -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings), "head")
    if ensure_key(settings.secret_key_file):
        log.warning(
            "created the secret key that encrypts the stored credential; back it up separately "
            "from the database backups",
            extra={"path": str(settings.secret_key_file)},
        )


def parse_grants(values: list[str]) -> dict[str, str]:
    grants = {}
    for value in values:
        group, sep, level = value.partition("=")
        if not sep:
            raise TokenError(f"expected GROUP=LEVEL, got {value!r}")
        grants[group] = level
    return grants


def cmd_create_token(args: argparse.Namespace, settings: Settings) -> None:
    sessions = make_sessionmaker(make_engine(settings))
    with sessions() as session, session.begin():
        token, secret = create_token(session, args.name, parse_grants(args.grant))
        result = {"name": token.name, "permissions": token.permissions, "token": secret}
    print(json.dumps(result, indent=2))
    print("Store the token now; it cannot be shown again.", file=sys.stderr)


def cmd_revoke_token(args: argparse.Namespace, settings: Settings) -> None:
    sessions = make_sessionmaker(make_engine(settings))
    with sessions() as session, session.begin():
        token = revoke_token(session, args.name)
        print(json.dumps({"name": token.name, "revoked_at": token.revoked_at.isoformat()}))


def cmd_list_tokens(_: argparse.Namespace, settings: Settings) -> None:
    sessions = make_sessionmaker(make_engine(settings))
    with sessions() as session, session.begin():
        tokens = [
            {
                "name": t.name,
                "prefix": t.prefix,
                "permissions": t.permissions,
                "created_at": t.created_at.isoformat(),
                "last_used_at": t.last_used_at and t.last_used_at.isoformat(),
                "revoked_at": t.revoked_at and t.revoked_at.isoformat(),
            }
            for t in session.scalars(select(ApiToken).order_by(ApiToken.created_at))
        ]
    print(json.dumps(tokens, indent=2))


def read_secret(prompt: str) -> str:
    """From the terminal without echo, or from a pipe. Never from an argument, which would stay
    in the shell history and in the process list."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline()


def cmd_connect_instagram(args: argparse.Namespace, settings: Settings) -> None:
    key = load_key(settings.secret_key_file)
    token = read_secret("Instagram access token (not shown): ").strip()
    if not token:
        raise SystemExit("error: no token given on standard input")
    clock = SystemClock()
    # "me" until the account is known: connecting only reads /me and renews the token.
    instagram = FakeInstagram(clock) if settings.simulation else InstagramClient(token, "me")
    connection = connect(
        make_sessionmaker(make_engine(settings)),
        instagram,
        token,
        key,
        clock.now(),
        replace=args.replace,
    )
    print(
        json.dumps(
            {
                "network": connection.network,
                "username": connection.username,
                "account_type": connection.account_type,
                "credential_expires_at": connection.credential_expires_at.isoformat(),
            },
            indent=2,
        )
    )


def cmd_serve(args: argparse.Namespace, settings: Settings) -> None:
    logs.configure()
    uvicorn.run(
        "jorgex_social.api:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_config=None,
    )


def acquire_worker_lock(settings: Settings):
    """Only one worker per database: two would both publish what is due, and a live attempt of
    one could look dead to the other. The lock is released when the process exits, even on a
    crash; keep the returned file open while the worker runs."""
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    path = settings.database_path.with_name(settings.database_path.name + ".worker.lock")
    lock = path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit(
            f"error: another worker is already running on this database ({path})"
        ) from None
    return lock


def cmd_worker(_: argparse.Namespace, settings: Settings) -> None:
    lock = acquire_worker_lock(settings)  # noqa: F841  (held until the process exits)
    logs.configure()
    stop = threading.Event()
    sessions = make_sessionmaker(make_engine(settings))
    clock = SystemClock()
    # One key for the client and the renewals, so a renewed credential is always readable.
    key = (
        None
        if settings.simulation and not settings.secret_key_file.exists()
        else load_key(settings.secret_key_file)
    )
    instagram = make_instagram(settings, clock, key)
    public_url = settings.public_url or (SIMULATED_PUBLIC_URL if settings.simulation else None)
    tasks = [
        PublishTask(
            instagram,
            clock,
            settings.media_dir,
            public_url,
            grace=timedelta(minutes=settings.publish_grace_minutes),
        ),
        SnapshotTask(instagram, clock),
        ConnectionTask(instagram, clock, key),
    ]
    # Setting the Event from a signal handler can deadlock with the loop waiting on it, so the
    # loop runs in a thread (which inherits the blocked mask) and the main thread waits for the
    # signal synchronously. `docker stop` sends SIGTERM.
    signals = {signal.SIGINT, signal.SIGTERM}
    signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    loop = threading.Thread(
        target=worker.run,
        args=(sessions, settings.worker_interval_seconds, stop, tasks),
        name="worker",
    )
    loop.start()
    signal.sigwait(signals)
    stop.set()
    loop.join()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jorgex-social")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply database migrations")

    token = sub.add_parser("create-token", help="create an API token and print it once")
    token.add_argument("--name", required=True)
    token.add_argument(
        "--grant",
        action="append",
        default=[],
        metavar="GROUP=LEVEL",
        help=(
            f"groups: {', '.join(g.value for g in Group)}; "
            f"levels: {', '.join(lv.value for lv in Level)}; ungranted groups are denied"
        ),
    )

    revoke = sub.add_parser("revoke-token", help="revoke an API token by name")
    revoke.add_argument("--name", required=True)

    sub.add_parser("list-tokens", help="list API tokens without their secrets")

    connect_ig = sub.add_parser(
        "connect-instagram",
        help="store an Instagram access token, read from standard input, encrypted",
    )
    connect_ig.add_argument(
        "--replace",
        action="store_true",
        help="switch this installation to a different account than the one connected",
    )

    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    sub.add_parser("worker", help="run the background worker")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    try:
        match args.command:
            case "migrate":
                logs.configure()
                migrate(settings)
            case "create-token":
                cmd_create_token(args, settings)
            case "revoke-token":
                cmd_revoke_token(args, settings)
            case "list-tokens":
                cmd_list_tokens(args, settings)
            case "connect-instagram":
                cmd_connect_instagram(args, settings)
            case "serve":
                cmd_serve(args, settings)
            case "worker":
                cmd_worker(args, settings)
    # Messages of these errors never carry the token (instagram/client.py, account/crypto.py).
    except (TokenError, SecretKeyError, NotConnected, ConnectError, InstagramError) as exc:
        raise SystemExit(f"error: {exc}") from None
