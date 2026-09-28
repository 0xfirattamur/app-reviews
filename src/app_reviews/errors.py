"""Exceptions this package raises."""

from __future__ import annotations


class AppReviewsError(Exception):
    """Base for everything this package raises."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthError(AppReviewsError):
    """Credentials were rejected, or could not be used."""


class HttpError(AppReviewsError):
    """A store request failed."""


class RateLimitError(HttpError):
    """HTTP 429.

    ``retry_after`` is the wait the store asked for, in seconds, or None when it
    sent no usable ``Retry-After``.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after


class NotFoundError(HttpError):
    """HTTP 404: the store has no such app."""


class RequestError(HttpError):
    """A completed request the store rejected permanently (HTTP 4xx)."""


class ReplyRejectedError(RequestError):
    """The store refused a reply, or it was refused before sending.

    ``reason`` is ``"too_long"`` for a Play reply over 350 characters, caught
    before any request. Otherwise it is the store's own error code when it sent
    one (``"ENTITY_ERROR.ATTRIBUTE.INVALID"``, ``"INVALID_ARGUMENT"``), else
    ``"http_<status>"``. Nothing was published.
    """

    def __init__(self, message: str, *, reason: str, status: int | None = None) -> None:
        super().__init__(message, status=status)
        self.reason = reason


class ReplyOutcomeUnknownError(HttpError):
    """A reply write was sent, and whether it took effect cannot be known.

    Raised for a timeout, a dropped connection or a 5xx on a write. The store may
    or may not have applied it, and writes are never retried automatically: a
    public reply cannot be taken back. Read the current state with
    ``get_reply()`` before deciding to send again.
    """


class ServerError(HttpError):
    """HTTP 5xx: the store failed on its own side."""


class TransportError(HttpError):
    """The exchange never completed, or returned nothing usable."""


class ParseError(HttpError):
    """A success carrying a body we could not read."""
