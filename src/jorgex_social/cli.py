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
from jorgex_social.access import operator
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
from jorgex_social.oauth import store as oauth
from jorgex_social.publishing.worker import SIMULATED_PUBLIC_URL, PublishTask
from jorgex_social.replies.task import ReplyTask
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


def cmd_list_grants(_: argparse.Namespace, settings: Settings) -> None:
    sessions = make_sessionmaker(make_engine(settings))
    with sessions() as session, session.begin():
        print(json.dumps(oauth.list_grants(session), indent=2))


def cmd_revoke_grant(args: argparse.Namespace, settings: Settings) -> None:
    """Ends grants and their tokens. Clients stay registered: a client that still holds its id
    would otherwise be stuck until it is logged out by hand (`claude mcp logout <name>`)."""
    sessions = make_sessionmaker(make_engine(settings))
    now = SystemClock().now()
    with sessions() as session, session.begin():
        if args.all:
            print(json.dumps({"revoked": oauth.revoke_all(session, now, "operator")}))
        else:
            grant = oauth.revoke_grant(session, args.id, now)
            print(json.dumps({"id": grant.id, "revoked_at": grant.revoked_at.isoformat()}))


def read_secret(prompt: str) -> str:
    """From the terminal without echo, or from a pipe. Never from an argument, which would stay
    in the shell history and in the process list."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline()


def read_new_password() -> str:
    password = read_secret("New operator password (not shown): ").rstrip("\r\n")
    if sys.stdin.isatty() and read_secret("Repeat it: ").rstrip("\r\n") != password:
        raise SystemExit("error: the passwords do not match")
    return password


def cmd_create_operator(_: argparse.Namespace, settings: Settings) -> None:
    password = read_new_password()
    sessions = make_sessionmaker(make_engine(settings))
    with sessions() as session, session.begin():
        operator.create_operator(session, password, SystemClock().now())
    print("Operator created. Run enroll-totp next: signing in needs the second factor.")


def cmd_set_password(_: argparse.Namespace, settings: Settings) -> None:
    password = read_new_password()
    sessions = make_sessionmaker(make_engine(settings))
    with sessions() as session, session.begin():
        operator.set_password(session, password, SystemClock().now())
    print("Password changed; every browser was signed out and every OAuth grant revoked.")


def cmd_enroll_totp(_: argparse.Namespace, settings: Settings) -> None:
    key = load_key(settings.secret_key_file)
    secret = operator.new_totp_secret()
    # The URI holds the secret: it goes to this terminal only, never to a log.
    print("Add this to your authenticator app (most can import the URI as text):")
    print(operator.provisioning_uri(secret))
    print(f"Or type the key by hand: {secret}")
    code = input("Code shown by the app: ") if sys.stdin.isatty() else sys.stdin.readline()
    sessions = make_sessionmaker(make_engine(settings))
    with sessions() as session, session.begin():
        operator.enroll_totp(session, key, secret, code, SystemClock().now())
    print("Second factor enrolled; every browser was signed out and every OAuth grant revoked.")


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
        # The client address behind the proxy: sign-in limits are per address, and without this
        # every request would share the proxy's.
        forwarded_allow_ips=settings.forwarded_allow_ips,
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
        # After the connection check, which in simulation is what records the account's id.
        ReplyTask(
            instagram,
            clock,
            poll_every=timedelta(seconds=settings.reply_poll_seconds),
            hourly_cap=settings.hourly_reply_cap,
            hourly_public_cap=settings.hourly_public_reply_cap,
            per_minute=settings.replies_per_minute,
            retention=timedelta(days=settings.retention_days),
            cooldown=timedelta(minutes=settings.reply_cooldown_minutes),
            webhook=settings.webhook_enabled,
        ),
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

    sub.add_parser("list-grants", help="list what OAuth clients were allowed, without tokens")
    revoke_grant = sub.add_parser(
        "revoke-grant", help="end an OAuth grant and its tokens; the client stays registered"
    )
    which = revoke_grant.add_mutually_exclusive_group(required=True)
    which.add_argument("--id", help="the grant's id, as list-grants shows it")
    which.add_argument("--all", action="store_true", help="every grant still in force")

    connect_ig = sub.add_parser(
        "connect-instagram",
        help="store an Instagram access token, read from standard input, encrypted",
    )
    connect_ig.add_argument(
        "--replace",
        action="store_true",
        help="switch this installation to a different account than the one connected",
    )

    sub.add_parser(
        "create-operator",
        help="create the web interface's operator, reading the password from standard input",
    )
    sub.add_parser("set-password", help="change the operator's password, read from standard input")
    sub.add_parser(
        "enroll-totp", help="set up the operator's second factor (TOTP), confirming one code"
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
            case "list-grants":
                cmd_list_grants(args, settings)
            case "revoke-grant":
                cmd_revoke_grant(args, settings)
            case "connect-instagram":
                cmd_connect_instagram(args, settings)
            case "create-operator":
                cmd_create_operator(args, settings)
            case "set-password":
                cmd_set_password(args, settings)
            case "enroll-totp":
                cmd_enroll_totp(args, settings)
            case "serve":
                cmd_serve(args, settings)
            case "worker":
                cmd_worker(args, settings)
    # Messages of these errors never carry the token (instagram/client.py, account/crypto.py).
    except (
        TokenError,
        oauth.OAuthError,
        operator.OperatorError,
        SecretKeyError,
        NotConnected,
        ConnectError,
        InstagramError,
    ) as exc:
        raise SystemExit(f"error: {exc}") from None
