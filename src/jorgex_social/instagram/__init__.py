"""Instagram adapter: the real Graph API client and a stateful fake with the same interface.

Everything specific to Instagram lives here; the rest of the service only sees these types.
"""

from jorgex_social.clock import Clock
from jorgex_social.config import Settings
from jorgex_social.instagram.client import InstagramClient
from jorgex_social.instagram.fake import FakeInstagram
from jorgex_social.instagram.types import Instagram


def network_for(settings: Settings) -> str:
    return FakeInstagram.network if settings.simulation else InstagramClient.network


def make_instagram(settings: Settings, clock: Clock) -> Instagram:
    if settings.simulation:
        return FakeInstagram(clock)
    if not (settings.instagram_access_token and settings.instagram_user_id):
        raise RuntimeError(
            "simulation is off but JORGEX_SOCIAL_INSTAGRAM_ACCESS_TOKEN or "
            "JORGEX_SOCIAL_INSTAGRAM_USER_ID is missing"
        )
    return InstagramClient(settings.instagram_access_token, settings.instagram_user_id)
