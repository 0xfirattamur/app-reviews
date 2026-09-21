"""Maps HTTP outcomes onto an ``ErrorKind``, and onto the class that carries it.

``classify`` answers as a string, for ``FetchError`` on the walk. ``error_for``
answers as an exception class, for the single-request path. Both share the one
status table, so the two deliveries of a failure can never classify it differently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app_reviews.errors import (
    AppReviewsError,
    AuthError,
    NotFoundError,
    ParseError,
    RateLimitError,
    RequestError,
    ServerError,
    TransportError,
)
from app_reviews.models.result import FetchError
from app_reviews.models.types import ErrorKind

if TYPE_CHECKING:
    from app_reviews.core.http import HttpResponse

_STATUS_KINDS: dict[int, ErrorKind] = {
    401: "auth",
    403: "auth",
    404: "not_found",
    429: "rate_limited",
}

_CLASS_FOR: dict[ErrorKind, type[AppReviewsError]] = {
    "rate_limited": RateLimitError,
    "auth": AuthError,
    "not_found": NotFoundError,
    "request": RequestError,
    "server": ServerError,
    "transport": TransportError,
    "parse": ParseError,
}
"""The one place the returned and the raised taxonomies are tied together.

Written out rather than derived from the classes, because the classes carry no
``kind``: the type is the classification. ``tests/app_reviews/test_errors.py``
pins these keys against ``get_args(ErrorKind)``, so a new kind cannot appear on
the data path without also appearing here.
"""


_KIND_FOR: dict[type[AppReviewsError], ErrorKind] = {
    cls: kind for kind, cls in _CLASS_FOR.items()
}
"""``_CLASS_FOR`` inverted, for a failure that arrives raised rather than returned.

Derived rather than written out, so the two directions cannot drift apart.
"""


def classify(
    status: int,
    transport_error: str | None = None,
    *,
    credentialed: bool = True,
) -> ErrorKind:
    """The kind of failure an HTTP outcome represents.

    ``status`` is 0 when the exchange never completed, in which case
    ``transport_error`` holds the exception text. A transport failure always wins
    over the status. Completed unmapped 4xx responses are permanent ``request``
    failures. A 401/403 is ``auth`` only for a credentialed endpoint.
    """
    if transport_error is not None or status == 0:
        return "transport"
    if status in {401, 403} and not credentialed:
        return "request"
    if kind := _STATUS_KINDS.get(status):
        return kind
    if status >= 500:
        return "server"
    if 400 <= status < 500:
        return "request"
    return "transport"


def error_for(
    status: int,
    transport_error: str | None = None,
    *,
    credentialed: bool = True,
) -> type[AppReviewsError]:
    """The exception class for an HTTP outcome.

    The raising twin of ``classify``, which returns the same decision as a string.
    Both go through ``classify``, so the status that yields
    ``FetchError(kind="rate_limited")`` on a walk raises ``RateLimitError`` on a
    single request.
    """
    return _CLASS_FOR[classify(status, transport_error, credentialed=credentialed)]


def fetch_error_from_response(
    *,
    country: str | None,
    status: int,
    message: str,
    transport_error: str | None = None,
    credentialed: bool = True,
) -> FetchError:
    """Build a classified FetchError from an HTTP outcome.

    ``message`` carries the real failure text: the exception string for a transport
    failure, a status description otherwise. A transport failure has ``status=0``,
    so describing it by status alone would report a meaningless ``"HTTP 0"``.
    """
    kind = classify(status, transport_error, credentialed=credentialed)
    return FetchError(
        country=country,
        message=message,
        kind=kind,
        status=status or None,
    )


def fetch_error_from_exception(
    *, country: str | None, exc: AppReviewsError
) -> FetchError:
    """Classify one of this package's own errors as page-walk data.

    A provider can fail by raising as well as by returning: obtaining a token is
    I/O of its own, and a token exchange that times out or 503s raises rather
    than producing a status for ``fetch_error_from_response`` to read. The walk
    converts those here, through the same table, so a failure means the same
    thing whichever way it arrived.

    ``AuthError`` never reaches this; see ``BaseReviews._page`` for why an
    unusable credential is raised instead of reported.

    An unmapped subclass falls back to its status, so ``HttpError(status=503)``
    still lands on ``server`` rather than on a guess.
    """
    for cls in type(exc).__mro__:
        if (kind := _KIND_FOR.get(cls)) is not None:
            break
    else:
        kind = classify(exc.status or 0)
    return FetchError(country=country, message=str(exc), kind=kind, status=exc.status)


def raise_for_http_failure(
    response: HttpResponse, api: str, *, credentialed: bool = True
) -> None:
    """Raise a classified ``HttpError`` unless the response is usable.

    The single-request twin of ``fetch_error_from_response``: same ``classify``
    call and the same vocabulary, delivered as an exception because search and
    lookup have one outcome rather than many. Both search clients carried a
    byte-identical private copy of this before it moved here.
    """
    if response.transport_error is not None:
        raise error_for(
            response.status,
            response.transport_error,
            credentialed=credentialed,
        )(
            f"{api} request failed: {response.transport_error}",
            status=response.status or None,
        )
    if not response.ok:
        raise error_for(response.status, credentialed=credentialed)(
            f"HTTP {response.status} from {api}", status=response.status
        )
