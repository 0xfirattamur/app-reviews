"""A request budget that several clients, threads and tasks can share."""

from __future__ import annotations

import logging
import math
import threading
from asyncio import sleep as asleep
from collections.abc import Awaitable
from time import monotonic, sleep
from typing import Protocol

__all__ = ["RateLimiter", "RequestLimiter"]

_LOG = logging.getLogger(__name__)


class RequestLimiter(Protocol):
    """What ``HttpClient`` needs from a limiter; ``RateLimiter`` is the default.

    ``acquire``/``aacquire`` run before every attempt, retries included;
    ``record`` runs after each response ``HttpClient`` reports. All three must be
    thread-safe: ``fetch`` runs countries on several threads.
    """

    def acquire(self) -> None:
        """Block the calling thread until one request may be sent."""
        ...

    def aacquire(self) -> Awaitable[None]:
        """Wait, without blocking the event loop, until one request may be sent."""
        ...

    def record(self, status: int, retry_after: float | None) -> None:
        """Learn from one response: its status and ``Retry-After`` seconds, or None."""
        ...


def _positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a finite number greater than 0")
    return float(value)


class RateLimiter:
    """A token bucket: ``rate`` tokens per second, holding at most ``burst``.

    Share one instance between every client that talks to a host to keep them
    all in one budget. A throttled answer (429 or 403) pauses every holder, for
    ``Retry-After`` or else ``initial_penalty`` doubling per consecutive throttle,
    capped at ``max_penalty``; a 2xx resets the doubling. The bucket starts full.
    """

    def __init__(
        self,
        rate: float,
        burst: int = 1,
        *,
        initial_penalty: float = 30.0,
        max_penalty: float = 900.0,
    ) -> None:
        self._rate = _positive(rate, "rate")
        if isinstance(burst, bool) or not isinstance(burst, int):
            raise TypeError("burst must be an integer")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self._initial_penalty = _positive(initial_penalty, "initial_penalty")
        self._max_penalty = _positive(max_penalty, "max_penalty")
        if self._max_penalty < self._initial_penalty:
            raise ValueError("max_penalty must be at least initial_penalty")
        self._burst = burst
        self._tokens = float(burst)
        self._refilled_at = monotonic()
        self._paused_until = 0.0
        self._throttle_streak = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Take one token, blocking this thread until one is available."""
        while (wait := self._take()) > 0:
            sleep(wait)

    async def aacquire(self) -> None:
        """Take one token, sleeping this task (not the loop) until one is available."""
        while (wait := self._take()) > 0:
            await asleep(wait)

    def penalize(self, seconds: float) -> None:
        """Hold every token back until ``seconds`` from now; never shortens a pause."""
        if isinstance(seconds, bool) or not isinstance(seconds, int | float):
            raise TypeError("seconds must be a number")
        if not (math.isfinite(seconds) and seconds >= 0):
            raise ValueError("seconds must be a finite number of at least 0")
        with self._lock:
            self._paused_until = max(self._paused_until, monotonic() + seconds)

    def record(self, status: int, retry_after: float | None) -> None:
        """Pause every holder on a 429 or 403; reset the throttle streak on a 2xx."""
        if 200 <= status < 300:
            with self._lock:
                self._throttle_streak = 0
            return
        if status not in (403, 429):
            return
        with self._lock:
            streak = self._throttle_streak
            self._throttle_streak += 1
        if retry_after is not None:
            delay = min(retry_after, self._max_penalty)
        else:
            doubled = self._initial_penalty * 2.0 ** min(streak, 64)
            delay = min(doubled, self._max_penalty)
        self.penalize(delay)
        _LOG.warning(
            "A throttled answer (status %d) paused the shared rate limiter for %.1fs",
            status,
            delay,
        )

    def _take(self) -> float:
        """Take a token and return 0, or return how long until one may be taken."""
        with self._lock:
            now = monotonic()
            if now < self._paused_until:
                return self._paused_until - now
            elapsed = max(0.0, now - self._refilled_at)
            self._tokens = min(float(self._burst), self._tokens + elapsed * self._rate)
            self._refilled_at = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            return (1.0 - self._tokens) / self._rate
