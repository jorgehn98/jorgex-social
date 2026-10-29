"""Instagram adapter: the real Graph API client and a stateful fake with the same interface.

Everything specific to Instagram lives here; the rest of the service only sees these types.
"""

from jorgex_social.account.credential import StoredCredential
from jorgex_social.account.crypto import load_key
from jorgex_social.clock import Clock
from jorgex_social.config import Settings
from jorgex_social.db import make_engine, make_sessionmaker
from jorgex_social.instagram.client import InstagramClient
from jorgex_social.instagram.fake import FakeInstagram
from jorgex_social.instagram.types import Instagram


def network_for(settings: Settings) -> str:
    return FakeInstagram.network if settings.simulation else InstagramClient.network


def make_instagram(settings: Settings, clock: Clock) -> Instagram:
    """In real mode the client reads the encrypted credential stored by `connect-instagram`;
    without the secret key or a connection it refuses to start and says why."""
    if settings.simulation:
        return FakeInstagram(clock)
    credential = StoredCredential(
        make_sessionmaker(make_engine(settings, write=False)),
        load_key(settings.secret_key_file),
        InstagramClient.network,
    )
    return InstagramClient(credential, credential.account_id())
