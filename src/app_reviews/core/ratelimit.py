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

    Any object with these three methods can be passed as ``rate_limiter=``, such
    as a limiter coordinated across processes. ``fetch`` runs countries on
    several threads, so all three must be safe to call concurrently.

    ``acquire`` (sync requests) or ``aacquire`` (async requests) is called before
    every attempt, retries included, and returns when the attempt may be sent.
    ``record`` is then called once for the response that attempt got, with its
    HTTP status and the server's ``Retry-After`` in seconds: never negative or
    NaN, and None when the header was absent or unreadable. Two exceptions: an
    attempt that got no response at all (connection failure, timeout) is not
    recorded, and neither is a 403 on a request that carried a credential, which
    is an authorization refusal rather than throttling.
    """

    def acquire(self) -> None:
        """Block the calling thread until one request may be sent."""
        ...

    def aacquire(self) -> Awaitable[None]:
        """Wait, without blocking the event loop, until one request may be sent."""
        ...

    def record(self, status: int, retry_after: float | None) -> None:
        """Learn from one response: its status and ``Retry-After`` seconds."""
        ...


def _positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a finite number greater than 0")
    return float(value)


class RateLimiter:
    """A token bucket: ``rate`` tokens per second, holding at most ``burst``.

    The default ``RequestLimiter``. Every request takes one token first, so one
    instance shared by every client that talks to a host keeps them all inside
    one budget, however many threads or tasks run them. ``acquire`` blocks the
    calling thread; ``aacquire`` sleeps only the calling task. Both draw from the
    same bucket.

    ``penalize`` is the brake for a host that has started refusing: it pauses
    every holder, not only the one that saw the refusal, because the host
    throttles the address, and a sibling request is what would extend the block.

    ``record`` applies it: a 429 or 403 is a throttled answer, and pauses the
    bucket for the server's ``Retry-After`` if it sent one, else for
    ``initial_penalty`` doubled for each consecutive throttled answer; either
    capped at ``max_penalty``. A 2xx ends the streak. The schedule belongs here
    rather than on ``RetryConfig`` because a store's block outlasts any wait worth
    spending inside one request.

    The bucket starts full. Waiters are not queued in arrival order: whoever
    re-checks first after a token appears takes it.
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
        """Hold every token back until ``seconds`` from now.

        Only ever extends: a shorter penalty arriving during a longer one leaves
        the longer one in place, so a burst of throttled answers cannot shorten
        the pause the first of them earned.
        """
        if isinstance(seconds, bool) or not isinstance(seconds, int | float):
            raise TypeError("seconds must be a number")
        if not (math.isfinite(seconds) and seconds >= 0):
            raise ValueError("seconds must be a finite number of at least 0")
        with self._lock:
            self._paused_until = max(self._paused_until, monotonic() + seconds)

    def record(self, status: int, retry_after: float | None) -> None:
        """Pause every holder on a 429 or 403; end the throttled streak on a 2xx.

        The server's ``Retry-After`` wins when it sent one. Otherwise the pause
        starts at ``initial_penalty`` and doubles for each consecutive throttled
        answer this limiter has seen. Either is capped at ``max_penalty``. Other
        statuses leave the limiter as it was.
        """
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
