import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Index, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column

from jorgex_social.db import Base, UTCDateTime


def _uuid() -> str:
    return str(uuid.uuid7())


class ConnectionStatus(StrEnum):
    OK = "ok"
    EXPIRING = "expiring"  # the credential expires in less than ten days
    EXPIRED = "expired"
    REVOKED = "revoked"  # the network rejects a credential that has not expired
    UNREADABLE = "unreadable"  # the stored credential does not decrypt with the secret key
    MISSING = "missing"  # no connection; reported by the API, never stored


class IncidentType(StrEnum):
    CREDENTIAL_EXPIRING = "credential_expiring"
    CREDENTIAL_EXPIRED = "credential_expired"
    PERMISSION_REVOKED = "permission_revoked"
    CREDENTIAL_UNREADABLE = "credential_unreadable"
    PERSISTENT_API_ERROR = "persistent_api_error"
    PUBLISH_UNRESOLVED = "publish_unresolved"
    # The webhook is configured, polling found activity it never delivered and it has been
    # silent for a day.
    WEBHOOK_SILENT = "webhook_silent"
    # The password was right and the code wrong, repeatedly: the password may have leaked. Closes
    # only when the credentials change.
    SIGN_IN_PASSWORD_ACCEPTED = "sign_in_password_accepted"
    # Instagram reported the account as restricted and the kill switch turned on by itself. Nothing
    # tells when that is over, so only the operator turning the switch off closes this one.
    ACCOUNT_RESTRICTED = "account_restricted"


class ActorType(StrEnum):
    TOKEN = "token"
    SYSTEM = "system"
    OPERATOR = "operator"  # commands run on the server, such as connect-instagram, and Settings


class Outcome(StrEnum):
    OK = "ok"
    DENIED = "denied"
    ERROR = "error"


class Connection(Base):
    """The connected account of one network: one row per network (one account per network)."""

    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    network: Mapped[str] = mapped_column(String(32), unique=True)
    external_account_id: Mapped[str] = mapped_column(String(64))
    username: Mapped[str] = mapped_column(String(100))
    account_type: Mapped[str | None] = mapped_column(String(32))
    # AES-GCM nonce + ciphertext (account/crypto.py). Never decrypted outside the worker and the
    # connect command. Null in simulation, whose fake needs no credential.
    credential_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    credential_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Meta only renews a credential older than 24 hours.
    credential_refreshed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16), default=ConnectionStatus.OK)
    # On Instagram, the publishing quota.
    limits: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Last notification from the network's webhook with a valid signature.
    webhook_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ChannelIncident(Base):
    """Something the operator or the agent should know about. Opens once while its cause lasts
    and closes when the cause is gone (on its own, except account_restricted); acknowledging it
    does not close it."""

    __tablename__ = "channel_incidents"
    __table_args__ = (
        Index("ix_channel_incidents_type_closed_at", "type", "closed_at"),
        # At most one open incident per cause, enforced by the database.
        Index(
            "uq_channel_incidents_open",
            "network",
            "type",
            unique=True,
            sqlite_where=text("closed_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    network: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(32))
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Identifiers and counts only: no secrets, no third-party text.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AuditEntry(Base):
    """Append-only (database triggers refuse updates and deletes), except that old denied
    sign-ins are purged. Never holds request bodies, secrets or third-party text."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_object", "object_type", "object_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_ref: Mapped[str | None] = mapped_column(String(32))  # the token prefix
    action: Mapped[str] = mapped_column(String(200))
    object_type: Mapped[str | None] = mapped_column(String(32))
    object_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
