from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jorgex_social.account import crypto
from jorgex_social.account.models import Connection


def credential_context(network: str) -> str:
    return f"connections/{network}/credential"


def seal(key: bytes, network: str, token: str) -> bytes:
    return crypto.encrypt(key, token, credential_context(network))


def connection_of(session: Session, network: str) -> Connection | None:
    return session.scalar(select(Connection).where(Connection.network == network))


class NotConnected(RuntimeError):
    pass


class StoredCredential:
    """The access token of the connected account, read and decrypted on every call: a renewal by
    the worker or a reconnection from the command line is used at once, and the plaintext never
    lives longer than one request."""

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

    def account_id(self) -> str:
        return self._connection().external_account_id

    def __call__(self) -> str:
        blob = self._connection().credential_encrypted
        return crypto.decrypt(self._key, blob, credential_context(self.network))
