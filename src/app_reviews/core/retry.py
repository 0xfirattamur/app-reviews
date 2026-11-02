"""Retry decisions: whether a failed request is tried again, and when."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app_reviews.models.config import RetryConfig

__all__ = ["RetryPolicy", "retry_after_seconds"]

_MAX_BACKOFF_EXPONENT = 1000
"""Largest power of two the backoff schedule computes.

``2.0 ** 1024`` overflows a float, and ``max_retries`` has no ceiling, so a long
schedule would raise instead of waiting. ``max_backoff`` caps the delay long
before this exponent matters; this only keeps the arithmetic finite.
"""


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
        exponent = min(attempt, _MAX_BACKOFF_EXPONENT)
        backoff = self._config.backoff_factor * 2.0**exponent
        return min(backoff, self._config.max_backoff)


def retry_after_seconds(value: str | None) -> float | None:
    """``Retry-After`` as seconds from now, or None if it is unusable.

    RFC 9110 allows either a delay in seconds or an HTTP date. Stores send
    seconds; the date form is accepted because it costs one stdlib call. A
    value already in the past, in either form, means "now".

    Untrusted text, so only what is certainly a delay is read as one: ASCII
    digits, optionally negative (a past instant, meaning "now"). Everything
    ``float()`` would also swallow (``1_0``, Arabic-Indic digits, ``nan``,
    ``inf``, ``1e400``) names no honest delay, and ``nan`` in particular made
    ``max(0.0, nan)`` return ``0.0``, so a throttled client retried with no
    backoff. Those fall through to the date form and then to None, where the
    exponential schedule applies. A date too large for the stdlib is None too.
    """
    if value is None or not (text := value.strip()):
        return None
    if _is_seconds(text):
        return max(0.0, float(text))
    try:
        when = parsedate_to_datetime(text)
    except (IndexError, TypeError, ValueError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(tz=UTC)).total_seconds())


def _is_seconds(text: str) -> bool:
    """Whether ``text`` is ASCII digits, after at most one leading ``-``."""
    digits = text.removeprefix("-")
    return digits.isascii() and digits.isdigit()
