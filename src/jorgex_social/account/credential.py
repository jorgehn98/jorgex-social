from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jorgex_social.account import crypto
from jorgex_social.account.models import Connection
from jorgex_social.instagram.errors import CredentialUnavailable


def credential_context(network: str) -> str:
    return f"connections/{network}/credential"


def seal(key: bytes, network: str, token: str) -> bytes:
    return crypto.encrypt(key, token, credential_context(network))


def connection_of(session: Session, network: str) -> Connection | None:
    return session.scalar(select(Connection).where(Connection.network == network))


class NotConnected(CredentialUnavailable):
    pass


class StoredCredential:
    """The access token and account id of the connected account, read together from one row and
    decrypted on every call: a renewal by the worker or a reconnection (even to another account)
    from the command line is used at once, and the plaintext never lives longer than one request.
    Any failure to read it is a CredentialUnavailable, raised before anything is sent."""

    def __init__(self, sessions: sessionmaker, key: bytes, network: str) -> None:
        self._sessions = sessions
        self._key = key
        self.network = network

    def __repr__(self) -> str:
        return f"StoredCredential(network={self.network!r})"

    def _connection(self) -> Connection:
        with self._sessions() as session:
            connection = connection_of(session, self.network)
        if connection is None or connection.credential_encrypted is None:
            raise NotConnected(
                f"no {self.network} connection; run `jorgex-social connect-instagram` first"
            )
        return connection

    def __call__(self) -> tuple[str, str]:
        connection = self._connection()
        try:
            token = crypto.decrypt(
                self._key, connection.credential_encrypted, credential_context(self.network)
            )
        except crypto.SecretKeyError as exc:
            raise CredentialUnavailable(str(exc)) from None
        return token, connection.external_account_id
