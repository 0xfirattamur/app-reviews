"""Retry decisions: whether a failed request is tried again, and when."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app_reviews.models.config import RetryConfig

__all__ = ["RetryPolicy", "retry_after_seconds"]


class RetryPolicy:
    """Decides whether a failed request is retried, and how long to wait first."""

    def __init__(self, config: RetryConfig) -> None:
        self._config = config
        self._retryable = frozenset(config.retry_on)

    def should_retry(self, attempt: int, status: int) -> bool:
        """True if this status is retryable and attempts remain.

        A status of 0 means the exchange never completed, which is always
        worth retrying while attempts remain.
        """
        if attempt >= self._config.max_retries:
            return False
        return status == 0 or status in self._retryable

    def get_delay(self, attempt: int, retry_after: str | None = None) -> float:
        """How long to wait before the next attempt.

        A server's ``Retry-After`` wins over the exponential schedule. It is the
        one party that knows when it will serve again, and coming back sooner than
        it asked is what turns throttling into a longer ban.

        Both paths are capped at ``max_backoff``, so neither an outsized header nor
        a high ``max_retries`` can park a request for hours.
        """
        asked = retry_after_seconds(retry_after)
        if asked is not None:
            return min(asked, self._config.max_backoff)
        return float(
            min(self._config.backoff_factor * (2**attempt), self._config.max_backoff)
        )


def retry_after_seconds(value: str | None) -> float | None:
    """``Retry-After`` as seconds from now, or None if it is unusable.

    RFC 9110 allows either a delay in seconds or an HTTP date. Stores send
    seconds; the date form is accepted because it costs one stdlib call. A
    value already in the past, in either form, means "now".

    NaN is rejected rather than clamped. It parses as a float, so it used to
    reach the clamp, where every comparison against it is False, making
    ``max(0.0, nan)`` return ``0.0`` and a throttled client retry with no
    backoff at all. Unlike ``-10`` it names no instant, so "unusable" is the
    honest reading and the exponential schedule applies. ``inf`` does name an
    instant and stays: callers cap it, and for a 429 the safe error is waiting
    too long.
    """
    if value is None or not (text := value.strip()):
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return None if math.isnan(seconds) else max(0.0, seconds)
    try:
        when = parsedate_to_datetime(text)
    except (IndexError, TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(tz=UTC)).total_seconds())
