"""Maps HTTP outcomes onto an ``ErrorKind``, and onto the class that carries it.

``classify`` answers as a string, for ``FetchError`` on the walk. ``error_for``
answers as an exception class, for the single-request path. Both share the one
status table, so the two deliveries of a failure can never classify it differently.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app_reviews.core.retry import retry_after_seconds
from app_reviews.errors import (
    AppReviewsError,
    AuthError,
    NotFoundError,
    ParseError,
    RateLimitError,
    ReplyOutcomeUnknownError,
    ReplyRejectedError,
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
        cls = error_for(response.status, credentialed=credentialed)
        message = f"HTTP {response.status} from {api}"
        if cls is RateLimitError:
            raise RateLimitError(
                message,
                status=response.status,
                retry_after=retry_after_seconds(response.retry_after),
            )
        raise cls(message, status=response.status)


STORE_ERROR_CHARS = 200
"""How much of a store's error text reaches an exception message."""


def raise_for_write_failure(response: HttpResponse, api: str) -> None:
    """Raise for a write the store did not accept, sorted by what the caller knows.

    A write is sent once and never retried (see ``HttpClient.post``), so the
    one question that matters is whether it may have taken effect:

    - no complete answer (``transport_error``) or a 5xx: it may have, so
      ``ReplyOutcomeUnknownError``;
    - 429: ``RateLimitError`` with the ``retry_after`` the store asked for;
    - 401/403: ``AuthError``, the credential cannot write here;
    - any other 4xx: ``ReplyRejectedError``, refused and not applied, with the
      store's error code as ``reason``.

    Every class but the first means nothing was published.
    """
    if response.transport_error is not None:
        raise ReplyOutcomeUnknownError(
            f"{api} write may or may not have been applied: "
            f"{response.transport_error}. Check get_reply() before sending again."
        )
    if response.ok:
        return
    status = response.status
    code, detail = _store_error(response.body)
    message = f"HTTP {status} from {api}" + (f": {detail}" if detail else "")
    if status >= 500:
        raise ReplyOutcomeUnknownError(
            f"{message}. The write may or may not have been applied; check "
            f"get_reply() before sending again.",
            status=status,
        )
    if status == 429:
        raise RateLimitError(
            message,
            status=status,
            retry_after=retry_after_seconds(response.retry_after),
        )
    if status in {401, 403}:
        raise AuthError(message, status=status)
    raise ReplyRejectedError(message, reason=code or f"http_{status}", status=status)


def _store_error(body: str) -> tuple[str | None, str | None]:
    """The ``(code, detail)`` of a store's JSON error body, where it has them.

    App Store Connect answers ``{"errors": [{"code", "detail"}]}`` and Google
    ``{"error": {"status", "message"}}``. Anything else yields ``(None, None)``;
    the status alone still classifies the failure. The detail is truncated
    because it is remote text bound for an exception message.
    """
    try:
        data = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    entry: Any = None
    if isinstance(errors := data.get("errors"), list) and errors:
        entry = errors[0]
        code_key, detail_key = "code", "detail"
    elif isinstance(data.get("error"), dict):
        entry = data["error"]
        code_key, detail_key = "status", "message"
    if not isinstance(entry, dict):
        return None, None
    code, detail = entry.get(code_key), entry.get(detail_key)
    return (
        code if isinstance(code, str) and code else None,
        detail[:STORE_ERROR_CHARS] if isinstance(detail, str) and detail else None,
    )
