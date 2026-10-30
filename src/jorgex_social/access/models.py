import uuid
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from jorgex_social.db import Base, UTCDateTime, utcnow


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid7()))
    name: Mapped[str] = mapped_column(String(100), unique=True)
    # Shown in listings so a token can be recognised without storing it.
    prefix: Mapped[str] = mapped_column(String(16))
    hash: Mapped[str] = mapped_column(String(64), unique=True)
    permissions: Mapped[dict[str, str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Operator(Base):
    """The one person who signs in to the web interface (decision 04). Its id never changes."""

    __tablename__ = "operator"
    __table_args__ = (CheckConstraint("singleton = 1", name="ck_operator_single_row"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid7()))
    # Always 1 and unique, so the database itself refuses a second operator.
    singleton: Mapped[int] = mapped_column(Integer, default=1, unique=True)
    # argon2id in its encoded form, which carries the algorithm and its parameters.
    password_hash: Mapped[str] = mapped_column(String(200))
    # AES-GCM with the master key (account/crypto.py); null until `enroll-totp` is confirmed.
    totp_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    # The last TOTP time step accepted: a code is never accepted twice, nor an older one.
    totp_last_step: Mapped[int | None] = mapped_column(Integer)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OperatorSession(Base):
    """A signed-in browser. Only the hash of the cookie value is stored."""

    __tablename__ = "sessions"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    operator_id: Mapped[str] = mapped_column(ForeignKey("operator.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    last_activity_at: Mapped[datetime] = mapped_column(UTCDateTime)
