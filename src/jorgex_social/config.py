import os
from dataclasses import dataclass, field
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
    # Until the in-app connection exists (build step 5), the real adapter reads these.
    instagram_access_token: str | None = field(default=None, repr=False)
    instagram_user_id: str | None = None

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
            instagram_access_token=env.get(f"{ENV_PREFIX}INSTAGRAM_ACCESS_TOKEN") or None,
            instagram_user_id=env.get(f"{ENV_PREFIX}INSTAGRAM_USER_ID") or None,
        )
