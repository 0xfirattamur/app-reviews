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
    """HTTP 429."""


class NotFoundError(HttpError):
    """HTTP 404: the store has no such app."""


class RequestError(HttpError):
    """A completed request the store rejected permanently (HTTP 4xx)."""


class ServerError(HttpError):
    """HTTP 5xx: the store failed on its own side."""


class TransportError(HttpError):
    """The exchange never completed, or returned nothing usable."""


class ParseError(HttpError):
    """A success carrying a body we could not read."""
