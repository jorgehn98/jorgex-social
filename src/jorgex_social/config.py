import os
from dataclasses import dataclass
from pathlib import Path

ENV_PREFIX = "JORGEX_SOCIAL_"


def _flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a boolean: {value!r}")


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
    # hour (Meta allows 750 private replies an hour; this stays well below), and how long events,
    # deliveries and conversations are kept.
    reply_poll_seconds: float = 120.0
    hourly_reply_cap: int = 200
    retention_days: int = 90
    # A message rule answers the same person at most once in this many minutes.
    reply_cooldown_minutes: int = 60

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        defaults = cls()
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
            retention_days=int(env.get(f"{ENV_PREFIX}RETENTION_DAYS", defaults.retention_days)),
            reply_cooldown_minutes=int(
                env.get(f"{ENV_PREFIX}REPLY_COOLDOWN_MINUTES", defaults.reply_cooldown_minutes)
            ),
        )
