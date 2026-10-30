import os
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import AnyUrl, ConfigDict, TypeAdapter

ENV_PREFIX = "JORGEX_SOCIAL_"
# Above the 7-day private-reply window plus the one-day margin the reply task keeps before
# retention (replies/task.py), so nothing still answerable is ever purged.
MIN_RETENTION_DAYS = 8


def _flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _positive(env, name: str, default: int) -> int:
    value = int(env.get(f"{ENV_PREFIX}{name}", default))
    if value < 1:
        raise ValueError(f"{ENV_PREFIX}{name} must be at least 1")
    return value


# The SDK's client metadata models parse URLs this way (url_preserve_empty_path).
_URL = TypeAdapter(AnyUrl, config=ConfigDict(url_preserve_empty_path=True))


def _https_uris(value: str) -> tuple[str, ...]:
    """Normalised as the OAuth SDK parses the redirect URIs a client registers (lower-case scheme
    and host, no default port; an empty path kept empty), so the exact comparison can match."""
    uris = []
    for entry in filter(None, (part.strip() for part in value.split(","))):
        uri = _URL.validate_python(entry)
        if uri.scheme != "https":
            raise ValueError(f"{ENV_PREFIX}OAUTH_REDIRECT_URIS takes https URIs only")
        uris.append(str(uri))
    return tuple(uris)


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path("data/jorgex-social.db")
    # A fresh install never calls the real network; going live is an explicit choice.
    simulation: bool = True
    worker_interval_seconds: float = 10.0
    # Uploaded media live here until seven days after they are published.
    media_dir: Path = Path("data/media")
    # Public HTTPS base URL of this service. Instagram downloads media from it, so publishing for
    # real needs it; simulation does not.
    public_url: str | None = None
    # A scheduled post found later than this (the worker was down) fails instead of going out late.
    publish_grace_minutes: int = 30
    # 32 random bytes that encrypt the stored Instagram credential. `migrate` creates it; keep it
    # out of the database backups, or a stolen backup carries everything needed to read the token.
    secret_key_file: Path = Path("data/secret.key")
    # Automatic replies: how often comments and messages are polled, the most replies sent in any
    # hour (Meta allows 750 private replies an hour; this stays well below), the most of those that
    # may be public (reported to be the riskier kind, on community evidence only), the most replies
    # sent in any minute (the rest wait for a later pass), and how long events, deliveries and
    # conversations are kept.
    reply_poll_seconds: float = 120.0
    hourly_reply_cap: int = 200
    hourly_public_reply_cap: int = 30
    replies_per_minute: int = 12
    retention_days: int = 90
    # A message rule answers the same person at most once in this many minutes.
    reply_cooldown_minutes: int = 60
    # /docs, /redoc and /openapi.json are always served in simulation; in real mode only when
    # this is set, so a public deployment does not describe its own API to anyone who asks.
    api_docs: bool = False
    # Meta webhook (POST /webhook). Both are needed to turn it on: the verify token the operator
    # generates and types into Meta's dashboard, and the app secret that signs every notification.
    # Kept out of repr so they never reach a log line or a traceback.
    webhook_verify_token: str | None = field(default=None, repr=False)
    meta_app_secret: str | None = field(default=None, repr=False)
    # A direct message the account sent by hand (not one of our replies) pauses automatic replies
    # to that person for this long.
    human_pause_minutes: int = 30
    # Peers whose X-Forwarded-For uvicorn believes (comma-separated addresses or networks): the
    # reverse proxy, as seen from the service. Behind Docker that is the network's gateway
    # (compose.yaml). Anyone else's header is ignored, so nobody can pick their own address.
    forwarded_allow_ips: str = "127.0.0.1,::1"
    # Sign-in attempts allowed per client address (IPv6: per /64), per minute and per hour.
    login_limit_per_minute: int = 5
    login_limit_per_hour: int = 20
    # https redirect URIs an OAuth client may register besides loopback ones (comma-separated,
    # matched exactly). Loopback (http://127.0.0.1, localhost, [::1]) is always allowed.
    oauth_redirect_uris: tuple[str, ...] = ()

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.webhook_verify_token and self.meta_app_secret)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        defaults = cls()
        retention_days = int(env.get(f"{ENV_PREFIX}RETENTION_DAYS", defaults.retention_days))
        if retention_days < MIN_RETENTION_DAYS:
            raise ValueError(f"{ENV_PREFIX}RETENTION_DAYS must be at least {MIN_RETENTION_DAYS}")
        return cls(
            database_path=Path(env.get(f"{ENV_PREFIX}DATABASE_PATH", defaults.database_path)),
            simulation=_flag(env.get(f"{ENV_PREFIX}SIMULATION", str(defaults.simulation))),
            worker_interval_seconds=float(
                env.get(f"{ENV_PREFIX}WORKER_INTERVAL_SECONDS", defaults.worker_interval_seconds)
            ),
            media_dir=Path(env.get(f"{ENV_PREFIX}MEDIA_DIR", defaults.media_dir)),
            public_url=(env.get(f"{ENV_PREFIX}PUBLIC_URL") or "").rstrip("/") or None,
            publish_grace_minutes=int(
                env.get(f"{ENV_PREFIX}PUBLISH_GRACE_MINUTES", defaults.publish_grace_minutes)
            ),
            secret_key_file=Path(env.get(f"{ENV_PREFIX}SECRET_KEY_FILE", defaults.secret_key_file)),
            reply_poll_seconds=float(
                env.get(f"{ENV_PREFIX}REPLY_POLL_SECONDS", defaults.reply_poll_seconds)
            ),
            hourly_reply_cap=int(
                env.get(f"{ENV_PREFIX}HOURLY_REPLY_CAP", defaults.hourly_reply_cap)
            ),
            hourly_public_reply_cap=int(
                env.get(f"{ENV_PREFIX}HOURLY_PUBLIC_REPLY_CAP", defaults.hourly_public_reply_cap)
            ),
            replies_per_minute=int(
                env.get(f"{ENV_PREFIX}REPLIES_PER_MINUTE", defaults.replies_per_minute)
            ),
            retention_days=retention_days,
            reply_cooldown_minutes=int(
                env.get(f"{ENV_PREFIX}REPLY_COOLDOWN_MINUTES", defaults.reply_cooldown_minutes)
            ),
            api_docs=_flag(env.get(f"{ENV_PREFIX}API_DOCS", str(defaults.api_docs))),
            webhook_verify_token=(env.get(f"{ENV_PREFIX}WEBHOOK_VERIFY_TOKEN") or "").strip()
            or None,
            meta_app_secret=(env.get(f"{ENV_PREFIX}META_APP_SECRET") or "").strip() or None,
            human_pause_minutes=int(
                env.get(f"{ENV_PREFIX}HUMAN_PAUSE_MINUTES", defaults.human_pause_minutes)
            ),
            forwarded_allow_ips=(env.get(f"{ENV_PREFIX}FORWARDED_ALLOW_IPS") or "").strip()
            or defaults.forwarded_allow_ips,
            login_limit_per_minute=_positive(
                env, "LOGIN_LIMIT_PER_MINUTE", defaults.login_limit_per_minute
            ),
            login_limit_per_hour=_positive(
                env, "LOGIN_LIMIT_PER_HOUR", defaults.login_limit_per_hour
            ),
            oauth_redirect_uris=_https_uris(env.get(f"{ENV_PREFIX}OAUTH_REDIRECT_URIS", "")),
        )
