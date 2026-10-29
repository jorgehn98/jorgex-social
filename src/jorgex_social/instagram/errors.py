class InstagramError(Exception):
    """Base error. `code` and `subcode` are Meta's; messages never include the access token."""

    def __init__(
        self, message: str, *, code: int | None = None, subcode: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode


class Refused(InstagramError):
    """Instagram answered and said no (4xx), so a write should not have happened.

    Before retrying a publish after any error, still check the container status: only
    PUBLISHED proves it went live, and that check is cheap.
    """

    def __init__(self, message: str, *, transient: bool = False, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.transient = transient


class RateLimited(Refused):
    """Too many calls; retry later. A write did not happen."""


class AuthError(Refused):
    """The credential is invalid or expired (code 190). Missing permissions arrive as Refused."""


class CredentialUnavailable(InstagramError):
    """The stored credential could not be read (no connection, or a secret key that does not
    decrypt it). Raised before anything is sent, so no write happened."""


class Unavailable(InstagramError):
    """No usable answer: 5xx, timeout or lost connection. A write may or may not have happened."""
