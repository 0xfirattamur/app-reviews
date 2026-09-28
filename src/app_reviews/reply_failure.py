"""Turns a reply write the store did not accept into the right exception.

Shared by the App Store and Google Play reply clients; each supplies a parser
for its own store's error body.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app_reviews.core.classify import http_error
from app_reviews.core.http import HttpResponse
from app_reviews.errors import ReplyOutcomeUnknownError, ReplyRejectedError

__all__ = ["StoreError", "json_object", "raise_for_reply_failure"]

DETAIL_CHARS = 200
"""How much of a store's error text reaches an exception message."""


@dataclass(frozen=True, slots=True)
class StoreError:
    """The error code and message a store's error body carried, where it had them."""

    code: str | None = None
    detail: str | None = None

    @classmethod
    def of(cls, code: object, detail: object) -> StoreError:
        """Keeps only non-empty strings; a store's body is untrusted remote JSON."""
        return cls(_text(code), _text(detail))


def json_object(body: str) -> dict[str, Any]:
    """``body`` as a JSON object, or ``{}`` when it is not one."""
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def raise_for_reply_failure(
    response: HttpResponse, api: str, parse_error: Callable[[str], StoreError]
) -> None:
    """Raise unless the store accepted the write sent with ``send_once``.

    - no answer, 3xx or 5xx: ``ReplyOutcomeUnknownError``, it may have applied;
    - 429, 401, 403: ``RateLimitError`` or ``AuthError``;
    - any other 4xx: ``ReplyRejectedError``, with the store's code as ``reason``.
    """
    if response.transport_error is not None:
        raise ReplyOutcomeUnknownError(
            f"{api} write may or may not have been applied: "
            f"{response.transport_error}. Check get_reply() before sending again."
        )
    if response.ok:
        return
    status = response.status
    error = parse_error(response.body)
    detail = error.detail[:DETAIL_CHARS] if error.detail else None
    message = f"HTTP {status} from {api}" + (f": {detail}" if detail else "")
    if 300 <= status < 400:
        raise ReplyOutcomeUnknownError(
            f"{message}: the write was redirected, and redirects are not followed "
            f"for writes because following one re-sends it. Check get_reply() "
            f"before sending again.",
            status=status,
        )
    if status >= 500:
        raise ReplyOutcomeUnknownError(
            f"{message}. The write may or may not have been applied; check "
            f"get_reply() before sending again.",
            status=status,
        )
    if status in {401, 403, 429}:
        raise http_error(response, message)
    raise ReplyRejectedError(
        message, reason=error.code or f"http_{status}", status=status
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
