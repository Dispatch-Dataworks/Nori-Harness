"""Exception hierarchy."""

from __future__ import annotations


class SkylightError(Exception):
    """Base class for every error raised by this package."""


class SkylightAuthError(SkylightError):
    """Login, token exchange or refresh failed, or the API rejected our token."""


class SkylightConfigError(SkylightError):
    """The client was not given enough information to make a request."""


class SkylightAPIError(SkylightError):
    """The API answered with an HTTP error status."""

    def __init__(self, message: str, *, status: int, method: str, path: str, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path
        self.body = body


class SkylightNotFoundError(SkylightAPIError):
    """404."""


class SkylightValidationError(SkylightAPIError):
    """422: the API rejected the payload."""


class SkylightRateLimitError(SkylightAPIError):
    """429."""

    def __init__(self, *args, retry_after: float | None = None, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after
