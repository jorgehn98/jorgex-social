"""Instagram adapter: the real Graph API client and a stateful fake with the same interface.

Everything specific to Instagram lives here; the rest of the service only sees these types.
"""

from jorgex_social.clock import Clock
from jorgex_social.config import Settings
from jorgex_social.db import make_engine, make_sessionmaker
from jorgex_social.instagram.client import InstagramClient
from jorgex_social.instagram.fake import FakeInstagram
from jorgex_social.instagram.types import Instagram


def network_for(settings: Settings) -> str:
    return FakeInstagram.network if settings.simulation else InstagramClient.network


def make_instagram(settings: Settings, clock: Clock, key: bytes | None = None) -> Instagram:
    """In real mode the client reads the encrypted credential stored by `connect-instagram`;
    without the secret key, with one that does not decrypt it, or without a connection, it
    refuses to start and says why."""
    if settings.simulation:
        return FakeInstagram(clock)
    # Imported here: the account module builds on this package (its errors), not the reverse.
    from jorgex_social.account.credential import StoredCredential
    from jorgex_social.account.crypto import load_key

    credential = StoredCredential(
        make_sessionmaker(make_engine(settings, write=False)),
        key or load_key(settings.secret_key_file),
        InstagramClient.network,
    )
    credential()  # decrypts once now: a wrong key stops the worker here, not mid-publish
    return InstagramClient.from_source(credential)
