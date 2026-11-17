import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from jorgex_social.account import crypto, scope
from jorgex_social.account.models import Connection
from jorgex_social.instagram.errors import CredentialUnavailable

log = logging.getLogger(__name__)


def credential_context(connection_id: str) -> str:
    """The associated data of one connection's credential: a ciphertext copied to another row
    does not decrypt there, even when both accounts are of the same network."""
    return f"connections/{connection_id}/credential"


def network_context(network: str) -> str:
    """The context before accounts indexed the schema, when the network named the row. Read only:
    a credential sealed with it is re-sealed with the connection's own on first use."""
    return f"connections/{network}/credential"


def seal(key: bytes, connection_id: str, token: str) -> bytes:
    return crypto.encrypt(key, token, credential_context(connection_id))


def unseal(key: bytes, connection: Connection) -> tuple[str, bool]:
    """The token of `connection` and whether it was sealed with the connection's own context,
    which it is not when it predates the schema being indexed by account. Raises
    crypto.SecretKeyError if neither context opens it.

    The network's context is only tried for a row that says its ciphertext is still sealed with it
    (`credential_legacy`): otherwise a ciphertext copied to another account of the same network
    would decrypt there."""
    blob = connection.credential_encrypted
    try:
        return crypto.decrypt(key, blob, credential_context(connection.id)), True
    except crypto.SecretKeyError:
        if not connection.credential_legacy:
            raise
        return crypto.decrypt(key, blob, network_context(connection.network)), False


class NotConnected(CredentialUnavailable):
    pass


class StoredCredential:
    """The access token and account id of the connected account, read together from one row and
    decrypted on every call: a renewal by the worker or a reconnection (even to another account)
    from the command line is used at once, and the plaintext never lives longer than one request.
    Any failure to read it is a CredentialUnavailable, raised before anything is sent.

    A credential sealed under its network's context is re-sealed under its connection's the first
    time it is read. The migration cannot do it: it does not have the master key."""

    def __init__(
        self,
        sessions: sessionmaker,
        key: bytes,
        network: str,
        writes: sessionmaker | None = None,
    ) -> None:
        self._sessions = sessions
        self._key = key
        # Where to store a credential re-sealed on read. Without it the read still works, and the
        # row keeps the old context until the credential is renewed or the account reconnected.
        self._writes = writes
        self.network = network

    def __repr__(self) -> str:
        return f"StoredCredential(network={self.network!r})"

    def _connection(self) -> Connection:
        with self._sessions() as session:
            connection = scope.account_of(session, self.network)
        if connection is None or connection.credential_encrypted is None:
            raise NotConnected(
                f"no {self.network} connection; run `jorgex-social connect-instagram` first"
            )
        return connection

    def __call__(self) -> tuple[str, str]:
        connection = self._connection()
        try:
            token, sealed_for_account = unseal(self._key, connection)
        except crypto.SecretKeyError as exc:
            raise CredentialUnavailable(str(exc)) from None
        if not sealed_for_account:
            self._reseal(connection, token)
        return token, connection.external_account_id

    def _reseal(self, connection: Connection, token: str) -> None:
        """Seals what was read with the connection's own context, best effort: the token is already
        usable, and a failure here is retried on the next read, which the flag in the row keeps
        asking for."""
        if self._writes is None:
            return
        try:
            with self._writes() as session, session.begin():
                stored: Connection | None = session.get(Connection, connection.id)
                # Only what was read: a credential renewed meanwhile is newer than this plaintext,
                # and it cleared the flag already.
                if (
                    stored is not None
                    and stored.credential_encrypted == connection.credential_encrypted
                ):
                    stored.credential_encrypted = seal(self._key, stored.id, token)
                    stored.credential_legacy = False
        except SQLAlchemyError as exc:
            log.warning(
                "could not re-seal the stored credential; read once more",
                extra={"error_type": type(exc).__name__},
            )
