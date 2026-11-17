"""Instagram adapter: the real Graph API client and a stateful fake with the same interface.

Everything specific to Instagram lives here; the rest of the service only sees these types.
"""

from typing import TYPE_CHECKING

from jorgex_social.clock import Clock
from jorgex_social.config import Settings
from jorgex_social.db import make_engine, make_sessionmaker
from jorgex_social.instagram.client import InstagramClient
from jorgex_social.instagram.fake import ACCOUNT_ID, FakeInstagram
from jorgex_social.instagram.types import Instagram

if TYPE_CHECKING:
    from jorgex_social.account.models import Connection


def network_for(settings: Settings) -> str:
    return FakeInstagram.network if settings.simulation else InstagramClient.network


def make_instagram(
    settings: Settings,
    clock: Clock,
    key: bytes | None,
    connection: Connection,
) -> Instagram:
    """The client of one connection. In real mode it reads and decrypts that row's credential on
    every call (`StoredCredential`); without the secret key, with one that does not decrypt it, or
    with no credential in the row, it refuses to start and says why. In simulation each connection
    gets its own fake, identified by the account id the row already holds."""
    if settings.simulation:
        return FakeInstagram(clock, account_id=connection.external_account_id or ACCOUNT_ID)
    # Imported here: the account module builds on this package (its errors), not the reverse.
    from jorgex_social.account.credential import StoredCredential
    from jorgex_social.account.crypto import load_key

    credential = StoredCredential(
        make_sessionmaker(make_engine(settings, write=False)),
        key or load_key(settings.secret_key_file),
        connection.id,
        # A credential sealed before the schema was indexed by account is re-sealed on first
        # read, which is a write: one short transaction, once.
        writes=make_sessionmaker(make_engine(settings)),
    )
    credential()  # decrypts once now: a wrong key stops this account's tasks, not mid-publish
    return InstagramClient.from_source(credential)


class Clients:
    """One Instagram client per connection, built the first time the account is seen and kept
    between passes: in simulation the fake *is* the account, so its state has to survive.

    In real mode every client reads its own row on each call, so a renewal or a reconnection
    applies at once without building it again. Building one account's client can fail (a
    credential that does not decrypt, a missing key); the caller logs it and the other accounts go
    on with theirs."""

    def __init__(self, settings: Settings, clock: Clock, key: bytes | None = None) -> None:
        self._settings = settings
        self._clock = clock
        self._key = key
        self._built: dict[str, Instagram] = {}

    def __call__(self, connection: Connection) -> Instagram:
        client = self._built.get(connection.id)
        if client is None:
            client = make_instagram(self._settings, self._clock, self._key, connection)
            self._built[connection.id] = client
        return client
