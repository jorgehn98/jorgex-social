"""The operator's sign-in to the web interface: password (argon2id), TOTP and server-side
sessions. Guessing is bounded per client address at the route (ui/routes.py), not per account:
a lockout of the one account would let anyone keep the operator out."""

import hmac
import logging
import re
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from jorgex_social.access.models import Operator, OperatorSession
from jorgex_social.access.tokens import hash_token
from jorgex_social.account import audit, crypto
from jorgex_social.account.models import ActorType, Outcome
from jorgex_social.oauth import store as oauth

log = logging.getLogger(__name__)

SESSION_LIFETIME = timedelta(hours=12)
IDLE_TIMEOUT = timedelta(hours=1)
# last_activity_at is only as precise as this, so browsing does not write on every page.
ACTIVITY_RESOLUTION = timedelta(minutes=1)
MIN_PASSWORD_LENGTH = 12
TOTP_CONTEXT = "operator/totp"
TOTP_ISSUER = "jorgex-social"
TOTP_CODE = re.compile(r"[0-9]{6}")  # ASCII only: \d would also take other scripts' digits
# Each argon2 check takes 64 MiB and a fraction of a second: at most this many at once, so a
# burst of sign-ins queues instead of exhausting memory, and only briefly, so it cannot tie up
# every worker thread either.
_password_checks = threading.BoundedSemaphore(2)
PASSWORD_CHECK_WAIT = 2.0  # seconds
# Each sign-in in flight holds one of the server's worker threads (40 in all) while it checks or
# waits. Past this many, a sign-in is refused at once instead of waiting, so a burst cannot take
# the threads every other synchronous endpoint (/health, the pages, the API) needs.
_password_attempts = threading.BoundedSemaphore(8)

# argon2id with the library's defaults (RFC 9106's low-memory profile).
_hasher = PasswordHasher()


class OperatorError(ValueError):
    pass


class Busy(Exception):
    """Every password check slot stayed taken for PASSWORD_CHECK_WAIT."""


def get_operator(session: Session) -> Operator | None:
    return session.scalar(select(Operator))


def _end_sessions(session: Session, now: datetime) -> None:
    """Every browser signs out and every OAuth grant ends: they were all given on the strength of
    the credentials that just changed."""
    session.execute(delete(OperatorSession))
    oauth.revoke_all(session, now, "credentials_changed")


def _hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise OperatorError(f"the password must have at least {MIN_PASSWORD_LENGTH} characters")
    return _hasher.hash(password)


def _audit(session: Session, now: datetime, action: str, operator: Operator, outcome=Outcome.OK):
    audit.record(
        session,
        now,
        action,
        outcome,
        actor_type=ActorType.OPERATOR,
        object_type="operator",
        object_id=operator.id,
    )


def create_operator(session: Session, password: str, now: datetime) -> Operator:
    if get_operator(session) is not None:
        raise OperatorError("the operator already exists; use set-password to change it")
    operator = Operator(password_hash=_hash_password(password), created_at=now)
    session.add(operator)
    session.flush()
    _audit(session, now, "operator.created", operator)
    return operator


def set_password(session: Session, password: str, now: datetime) -> Operator:
    """Also signs every browser out and ends every OAuth grant."""
    operator = get_operator(session)
    if operator is None:
        raise OperatorError("there is no operator yet; run create-operator first")
    operator.password_hash = _hash_password(password)
    _end_sessions(session, now)
    _audit(session, now, "operator.password_set", operator)
    return operator


def new_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name="operator", issuer_name=TOTP_ISSUER)


def totp_step(secret: str, code: str, now: datetime, after: int | None) -> int | None:
    """The time step `code` belongs to, allowing one step of clock drift either way, or None.
    Steps up to `after` are refused, so a code (or an older one) is never accepted twice."""
    code = code.strip()
    if not TOTP_CODE.fullmatch(code):
        # Anything else is just a wrong code. compare_digest would raise on non-ASCII text, and
        # an error only after a right password would tell the password apart.
        return None
    totp = pyotp.TOTP(secret)
    current = totp.timecode(now)
    for step in (current - 1, current, current + 1):
        if after is not None and step <= after:
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None


def enroll_totp(session: Session, key: bytes, secret: str, code: str, now: datetime) -> None:
    """Stores `secret` only once `code` proves the authenticator app has it. Signs every browser
    out and ends every OAuth grant, since the old second factor no longer applies."""
    operator = get_operator(session)
    if operator is None:
        raise OperatorError("there is no operator yet; run create-operator first")
    step = totp_step(secret, code, now, None)
    if step is None:
        raise OperatorError("the code does not match; check the device's clock and try again")
    operator.totp_secret_encrypted = crypto.encrypt(key, secret, TOTP_CONTEXT)
    operator.totp_last_step = step  # the confirmation code cannot then sign in
    _end_sessions(session, now)
    _audit(session, now, "operator.totp_enrolled", operator)


def _password_matches(password_hash: str, password: str) -> bool:
    if not _password_attempts.acquire(blocking=False):
        raise Busy
    try:
        if not _password_checks.acquire(timeout=PASSWORD_CHECK_WAIT):
            raise Busy
        try:
            return _hasher.verify(password_hash, password)
        except VerificationError, InvalidHashError:
            return False
        finally:
            _password_checks.release()
    finally:
        _password_attempts.release()


def log_in(
    write: sessionmaker,
    read: sessionmaker,
    key_file: Path,
    password: str,
    code: str,
    now: datetime,
) -> str | None:
    """Returns a new session id, or None whatever the reason, so the caller cannot tell the
    reasons apart. Raises Busy when no password check slot frees up in time. The checks read
    in a deferred transaction and run outside any transaction; the write lock is taken only to
    record the outcome, and never when there is no operator or the code is malformed."""
    if not TOTP_CODE.fullmatch(code.strip()):
        # Not even shaped like a code (the form only sends six digits): refused before paying
        # for an argon2 check. It says nothing about the password, so it needs no record.
        return None
    with read() as session, session.begin():
        operator = get_operator(session)
    if operator is None:
        return None
    step = None
    if _password_matches(operator.password_hash, password):
        if operator.totp_secret_encrypted is None:
            log.warning("sign-in refused: the operator has no TOTP enrolled; run enroll-totp")
        else:
            secret = crypto.decrypt(
                crypto.load_key(key_file), operator.totp_secret_encrypted, TOTP_CONTEXT
            )
            step = totp_step(secret, code, now, operator.totp_last_step)
    with write() as session, session.begin():
        # Read again under the write lock: a concurrent sign-in may have used the same code or
        # changed the password since.
        current = get_operator(session)
        if current is None:
            return None
        still_valid = (
            current.password_hash == operator.password_hash
            and current.totp_last_step == operator.totp_last_step
        )
        if step is not None and still_valid:
            current.totp_last_step = step
            session_id = secrets.token_urlsafe(32)
            session.execute(delete(OperatorSession).where(OperatorSession.expires_at <= now))
            session.add(
                OperatorSession(
                    id_hash=hash_token(session_id),
                    operator_id=current.id,
                    created_at=now,
                    expires_at=now + SESSION_LIFETIME,
                    last_activity_at=now,
                )
            )
            _audit(session, now, "operator.login", current)
            return session_id
        _audit(session, now, "operator.login", current, Outcome.DENIED)
        return None


def find_session(session: Session, session_id: str, now: datetime) -> OperatorSession | None:
    """Only reads. Expired and idle sessions count as absent."""
    found = session.get(OperatorSession, hash_token(session_id))
    if found is None or found.expires_at <= now or now - found.last_activity_at >= IDLE_TIMEOUT:
        return None
    return found


def needs_activity_recorded(found: OperatorSession, now: datetime) -> bool:
    return now - found.last_activity_at >= ACTIVITY_RESOLUTION


def record_activity(session: Session, found: OperatorSession, now: datetime) -> None:
    # Guarded, so concurrent pages of the same browser write at most once per resolution.
    session.execute(
        update(OperatorSession)
        .where(OperatorSession.id_hash == found.id_hash)
        .where(OperatorSession.last_activity_at <= now - ACTIVITY_RESOLUTION)
        .values(last_activity_at=now)
    )


def is_stored(session: Session, session_id: str) -> bool:
    """Only reads; expired sessions count, so signing out still removes them."""
    return session.get(OperatorSession, hash_token(session_id)) is not None


def log_out(session: Session, session_id: str) -> None:
    by_id = OperatorSession.id_hash == hash_token(session_id)
    session.execute(delete(OperatorSession).where(by_id))
