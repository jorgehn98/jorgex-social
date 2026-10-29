"""Connecting the account, renewing its credential and checking the connection's health."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from jorgex_social.account import audit, crypto, incidents
from jorgex_social.account.credential import connection_of, credential_context, seal
from jorgex_social.account.models import (
    ActorType,
    Connection,
    ConnectionStatus,
    IncidentType,
    Outcome,
)
from jorgex_social.clock import Clock
from jorgex_social.instagram.errors import (
    AuthError,
    CredentialUnavailable,
    InstagramError,
    Refused,
)
from jorgex_social.instagram.fake import FakeInstagram
from jorgex_social.instagram.types import Account, Instagram, PublishingLimit
from jorgex_social.publishing.models import AttemptResult, Post, PostStatus, PublishAttempt

log = logging.getLogger(__name__)

CHECK_EVERY = timedelta(hours=1)
EXPIRING_WITHIN = timedelta(days=10)
# A long-lived token lasts 60 days. Renewing below 50 (some ten days after the last renewal)
# leaves weeks to retry before EXPIRING_WITHIN, let alone expiry.
RENEW_BELOW = timedelta(days=50)
# Meta only renews a token at least this old (docs/research/references/instagram.md).
MIN_AGE_TO_RENEW = timedelta(hours=24)
PERSISTENT_ERRORS = 3
UNRESOLVED_AFTER = timedelta(hours=1)
# Expiry assumed for a token Meta would not renew at connect time (younger than 24 hours).
ESTIMATED_LIFETIME = timedelta(days=59)


class ConnectError(Exception):
    pass


def status_at(connection: Connection, now: datetime) -> ConnectionStatus:
    """Expiry is known without asking the network, so the status is right between checks."""
    expires_at = connection.credential_expires_at
    if expires_at is not None and expires_at <= now:
        return ConnectionStatus.EXPIRED
    if connection.status in {ConnectionStatus.REVOKED, ConnectionStatus.UNREADABLE}:
        return ConnectionStatus(connection.status)
    if expires_at is not None and expires_at - now < EXPIRING_WITHIN:
        return ConnectionStatus.EXPIRING
    return ConnectionStatus.OK


def should_renew(connection: Connection, now: datetime) -> bool:
    expires_at, refreshed_at = connection.credential_expires_at, connection.credential_refreshed_at
    if connection.credential_encrypted is None or expires_at is None or expires_at <= now:
        return False  # nothing to renew, or expired: Meta does not renew an expired token
    if refreshed_at is not None and now - refreshed_at < MIN_AGE_TO_RENEW:
        return False
    return expires_at - now < RENEW_BELOW


def _limits(limit: PublishingLimit) -> dict:
    return {
        "quota_usage": limit.quota_usage,
        "quota_total": limit.quota_total,
        "quota_duration_seconds": limit.quota_duration_seconds,
    }


def connect(
    sessions: sessionmaker,
    instagram: Instagram,
    token: str,
    key: bytes,
    now: datetime,
    *,
    replace: bool = False,
) -> Connection:
    """Stores the credential `instagram` was built with (`token`), after checking which account
    it opens and renewing it once: the renewal is how its expiry becomes known. `replace` lets
    the installation switch to a different account; without it, that is refused."""
    network = instagram.network
    account = instagram.account()
    with sessions() as session:
        current = connection_of(session, network)
    switching = current is not None and current.external_account_id != account.id
    if switching and not replace:
        raise ConnectError(
            f"this installation is connected to another {network} account; one installation "
            "serves one account (use --replace to switch accounts)"
        )
    try:
        renewal = instagram.refresh_token()
        stored, expires_at, estimated = renewal.access_token, now + renewal.expires_in, False
    except AuthError:
        raise
    except Refused as exc:
        # Meta does not renew a token younger than 24 hours, so a token generated today is
        # refused. Such a token has at most 60 days left; 59 errs on the early side, and the
        # worker renews it once it is old enough.
        stored, expires_at, estimated = token, now + ESTIMATED_LIFETIME, True
        log.warning(
            "Instagram did not renew the token; its expiry is estimated",
            extra={"error": str(exc), "expires_at": expires_at},
        )
    with sessions() as session, session.begin():
        connection = connection_of(session, network)
        if connection is None:
            connection = Connection(network=network)
            session.add(connection)
        connection.external_account_id = account.id
        connection.username = account.username
        connection.account_type = account.account_type
        connection.credential_encrypted = seal(key, network, stored)
        connection.credential_expires_at = expires_at
        connection.credential_refreshed_at = now
        connection.status = ConnectionStatus.OK
        connection.limits = None if switching else connection.limits
        connection.checked_at = now
        session.flush()
        audit.record(
            session,
            now,
            "connection.replaced" if switching else "connection.connected",
            actor_type=ActorType.OPERATOR,
            object_type="connection",
            object_id=connection.id,
            detail={"expiry_estimated": True} if estimated else None,
        )
    log.info("connected", extra={"network": network, "username": account.username})
    return connection


class ConnectionTask:
    """Worker task: checks the connection at most once an hour (renewing the credential when
    due) and keeps the incidents in step with what it finds. Watching for publish outcomes left
    unresolved needs no network, so it runs on every pass."""

    __name__ = "connection"

    def __init__(self, instagram: Instagram, clock: Clock, key: bytes | None) -> None:
        """`key` is the one the worker's client decrypts with: renewing with any other would
        store a credential the client cannot read. None only in simulation without a key."""
        self.instagram = instagram
        self.clock = clock
        self.key = key
        self._last_check: datetime | None = None
        self._consecutive_errors = 0

    def __call__(self, sessions: sessionmaker) -> None:
        now = self.clock.now()
        self.watch_unresolved(sessions, now)
        if self._last_check and now - self._last_check < CHECK_EVERY:
            return
        self._last_check = now
        self.check(sessions, now)

    def check(self, sessions: sessionmaker, now: datetime) -> None:
        network = self.instagram.network
        with sessions() as session:
            connection = connection_of(session, network)
        if connection is None and network != FakeInstagram.network:
            return  # nothing connected; the API reports it as missing
        unreadable = False
        if connection is not None and should_renew(connection, now):
            unreadable = not self._renew(sessions, connection, now)

        account: Account | None = None
        limits: dict | None = None
        auth_failed = False
        try:
            answered = self.instagram.account()
            limits = _limits(self.instagram.publishing_limit())
            account = answered
        except CredentialUnavailable as exc:
            unreadable = True
            log.error("stored credential unreadable", extra={"error": str(exc)})
        except AuthError:
            auth_failed = True
        except InstagramError as exc:
            log.warning("connection check failed", extra={"error": str(exc)})

        if account is not None:
            self._consecutive_errors = 0
        elif not (auth_failed or unreadable):
            self._consecutive_errors += 1
        with sessions() as session, session.begin():
            connection = connection_of(session, network)
            if connection is None and account is not None:
                # Simulation: the fake needs no credential, so its connection appears on its own.
                connection = Connection(network=network, external_account_id=account.id)
                session.add(connection)
            if connection is not None:
                if unreadable:
                    connection.status = ConnectionStatus.UNREADABLE
                elif account is not None:
                    connection.username = account.username
                    connection.account_type = account.account_type
                    connection.limits = limits
                    connection.status = ConnectionStatus.OK  # a revoked credential works again
                elif auth_failed:
                    connection.status = ConnectionStatus.REVOKED  # EXPIRED below if expired
                connection.status = status_at(connection, now)
                connection.checked_at = now
                status = connection.status
            else:
                status = ConnectionStatus.REVOKED if auth_failed else None
            for type_, active in (
                (IncidentType.CREDENTIAL_EXPIRING, status == ConnectionStatus.EXPIRING),
                (IncidentType.CREDENTIAL_EXPIRED, status == ConnectionStatus.EXPIRED),
                (IncidentType.PERMISSION_REVOKED, status == ConnectionStatus.REVOKED),
                (IncidentType.CREDENTIAL_UNREADABLE, status == ConnectionStatus.UNREADABLE),
            ):
                incidents.sync(session, network, type_, active, now)
            incidents.sync(
                session,
                network,
                IncidentType.PERSISTENT_API_ERROR,
                self._consecutive_errors >= PERSISTENT_ERRORS,
                now,
                {"consecutive_failures": self._consecutive_errors},
            )

    def _renew(self, sessions: sessionmaker, before: Connection, now: datetime) -> bool:
        """Returns False when the stored credential does not decrypt with the worker's key; then
        Meta is not asked, since a renewed token could not be stored readably."""
        network = self.instagram.network
        context = credential_context(network)
        if self.key is None:
            log.error("no secret key: the credential cannot be renewed")
            return False
        try:
            crypto.decrypt(self.key, before.credential_encrypted, context)
        except crypto.SecretKeyError:
            return False
        try:
            renewal = self.instagram.refresh_token()
        except InstagramError as exc:
            # The check that follows records an invalid credential; anything else is retried
            # on the next check, weeks before the credential expires.
            log.warning("credential renewal failed", extra={"error": str(exc)})
            with sessions() as session, session.begin():
                audit.record(
                    session,
                    now,
                    "credential.renewed",
                    Outcome.ERROR,
                    object_type="connection",
                    object_id=before.id,
                    detail={"error_code": exc.code},
                )
            return True
        with sessions() as session, session.begin():
            connection = connection_of(session, network)
            if connection is None or connection.credential_encrypted != before.credential_encrypted:
                # Reconnected (maybe to another account) while Meta answered: the stored
                # credential is newer than the one renewed, so the renewal is dropped.
                log.warning("connection changed during renewal; renewal discarded")
                audit.record(
                    session,
                    now,
                    "credential.renewed",
                    Outcome.ERROR,
                    object_type="connection",
                    object_id=before.id,
                    detail={"discarded": "connection_changed"},
                )
                return True
            connection.credential_encrypted = seal(self.key, network, renewal.access_token)
            connection.credential_expires_at = now + renewal.expires_in
            connection.credential_refreshed_at = now
            audit.record(
                session,
                now,
                "credential.renewed",
                object_type="connection",
                object_id=connection.id,
            )
        log.info("credential renewed", extra={"expires_at": now + renewal.expires_in})
        return True

    def watch_unresolved(self, sessions: sessionmaker, now: datetime) -> None:
        """Posts whose outcome the resolver has not settled for over an hour need a human."""
        network = self.instagram.network
        with sessions() as session, session.begin():
            post_ids = session.scalars(
                select(Post.id)
                .join(PublishAttempt)
                .where(
                    Post.network == network,
                    Post.status == PostStatus.OUTCOME_UNKNOWN,
                    PublishAttempt.result == AttemptResult.UNKNOWN,
                    PublishAttempt.resolved_at.is_(None),
                    PublishAttempt.ended_at <= now - UNRESOLVED_AFTER,
                )
                .distinct()
                .order_by(Post.id)
            ).all()
            incidents.sync(
                session,
                network,
                IncidentType.PUBLISH_UNRESOLVED,
                bool(post_ids),
                now,
                {"post_ids": list(post_ids)},
            )
