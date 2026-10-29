class InstagramError(Exception):
    """Base error. `code` and `subcode` are Meta's; messages never include the access token."""

    def __init__(
        self, message: str, *, code: int | None = None, subcode: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode


class Refused(InstagramError):
    """Instagram answered and said no (4xx). A write did not happen."""

    def __init__(self, message: str, *, transient: bool = False, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.transient = transient


class RateLimited(Refused):
    """Too many calls; retry later. A write did not happen."""


class AuthError(Refused):
    """The credential is invalid, expired or lacks a permission (code 190)."""


class Unavailable(InstagramError):
    """No usable answer: 5xx, timeout or lost connection. A write may or may not have happened."""
