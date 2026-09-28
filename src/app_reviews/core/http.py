"""httpx-based request execution with retry and proxy support."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from typing import Any

import httpx

from app_reviews.core.ratelimit import RequestLimiter
from app_reviews.core.retry import RetryPolicy, retry_after_seconds
from app_reviews.models.config import RetryConfig

_LOG = logging.getLogger(__name__)

_PERMANENT_EXCEPTIONS = (httpx.InvalidURL,)
"""Failures that will fail the same way on every attempt.

A malformed URL never completes an exchange, which looks exactly like a
connection failure (``status=0``), and ``should_retry`` treats that as always
worth another go. It is not: the tenth attempt parses the same string, so the
whole backoff schedule is spent before the caller is told.
"""

_TRANSPORT_EXCEPTIONS = (httpx.HTTPError, *_PERMANENT_EXCEPTIONS)
"""What a ``client.get``/``client.post`` call raises for a failed exchange.

``httpx.HTTPError`` covers every ``RequestError`` subclass (connection failures,
timeouts, protocol errors, too-many-redirects) plus ``HTTPStatusError``. It does
not cover ``httpx.InvalidURL``, which subclasses ``Exception`` directly, so a
malformed URL (``"http://[::1"``, or one with a stray tab) raises past a bare
``except httpx.HTTPError``. httpx parses the URL on every request call, which is
what makes ``InvalidURL`` reachable here.

Deliberately excluded: ``httpx.CookieConflict``, raised only by explicit
``Cookies.get()`` lookups. That would mean a bug in this code rather than a
transport failure, and must propagate.

``httpx.StreamError`` subclasses ``HTTPError`` and so *is* covered. This module
does stream every response (see ``_read``), and a stream consumed twice or read
after closing is a bug here, not a transport failure, but the family also
covers genuine mid-body truncation, which is. Treating the pair as retryable
transport failures is the safer default: a real bug in ``_read`` fails loudly in
the suite long before it reaches a retry.
"""

DEFAULT_MAX_BYTES = 32 * 1024 * 1024
"""Bytes of one response body this client will hold in memory.

Not the server's choice to make. Every body here is buffered whole, so an
endpoint that answers with an endless stream would grow the process until it
died. Set far above any real page (the largest Play ``batchexecute`` response is
a few MB), so the cap is only ever reached by something broken or hostile.
"""

DEFAULT_MAX_DURATION = 120.0
"""Wall-clock seconds one attempt gets, body read included.

``timeout`` is per socket operation, which cannot bound an exchange: a server
answering each read a shade inside the read timeout holds the connection open
indefinitely without ever timing out. This is the ceiling on the whole attempt,
so a trickle ends.
"""


class _BodyTooLarge(Exception):
    """A response body exceeded ``max_bytes``. Permanent: a retry re-reads it."""


class _AttemptTooSlow(Exception):
    """An attempt outlived ``max_duration``. Permanent: retrying re-waits it."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """The outcome of one HTTP exchange.

    ``status`` is 0 and ``transport_error`` is set when the exchange never
    completed, such as a connection failure or timeout. In that case ``body`` is
    empty and ``transport_error`` holds the real exception text.
    """

    status: int
    body: str
    transport_error: str | None = None

    @property
    def ok(self) -> bool:
        """True for a 2xx response."""
        return 200 <= self.status < 300

    def json(self) -> Any:
        """Parse body as JSON."""
        return json.loads(self.body)


class HttpClient:
    """Owns one connection pool, and is the seam tests inject through.

    Two jobs. Connection reuse: one pool for this client's lifetime, so a ten-page
    walk costs one TLS handshake rather than ten. And the injection seam:
    ``transport`` is a constructor parameter of the object that actually performs
    the I/O, which is where a test can substitute one.

    ``timeout``, ``proxy``, ``retry`` and ``rate_limiter`` are client state
    rather than per-call arguments: they describe how this client talks to a
    host, not what any one request wants.

    With a ``rate_limiter``, every attempt (retries included) takes a token first
    and reports its response to the limiter afterwards; ``retry`` still governs
    only the waits between attempts of one request.

    Both pools are lazy and independent, so a sync-only caller never constructs
    an ``AsyncClient`` (which would want a running loop) and vice versa. Building
    either is guarded by a lock: ``fetch`` fans out across threads on one shared
    client, and a bare check-then-set let concurrent first use build several pools
    and keep only the last, leaving the rest open with nothing able to close them.
    ``httpx.Client`` itself is safe to *use* from many threads.

    Redirects are followed because the iTunes endpoints need them. That is safe for
    credentialed requests only because httpx drops ``Authorization`` on any origin
    change; ``tests/app_reviews/core/test_http.py`` pins that, since it is
    behaviour of a pinned dependency rather than of this code.
    """

    USER_AGENT = "app-reviews"
    """Sent on every request unless the caller overrides it."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        proxy: str | None = None,
        retry: RetryConfig | None = None,
        transport: Any = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_duration: float = DEFAULT_MAX_DURATION,
        rate_limiter: RequestLimiter | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_duration = max_duration
        self._proxy = proxy
        self._retry = retry
        self._policy = RetryPolicy(retry) if retry else None
        self._limiter = rate_limiter
        self._transport = transport
        self._sync_pool: httpx.Client | None = None
        self._async_pool: httpx.AsyncClient | None = None
        self._pool_lock = threading.Lock()

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> HttpResponse:
        """Perform a GET on the shared pool."""
        pool = self._pool()
        return self._execute(
            "GET",
            url,
            send=lambda: pool.stream(
                "GET", url, headers=self._headers(headers), params=params
            ),
            credentialed=_carries_credential(headers),
        )

    def post(
        self,
        url: str,
        *,
        body: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        """Perform a POST on the shared pool.

        Pass ``follow_redirects=False`` when the body carries a credential. httpx
        drops ``Authorization`` when the origin changes, but a 307/308 re-sends
        the *body* verbatim to the next host, and an OAuth assertion lives there.
        """
        pool = self._pool()
        return self._execute(
            "POST",
            url,
            send=lambda: pool.stream(
                "POST",
                url,
                content=body,
                headers=self._headers(headers),
                follow_redirects=follow_redirects,
            ),
            credentialed=not follow_redirects or _carries_credential(headers),
        )

    async def aget(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> HttpResponse:
        """Async equivalent of ``get``."""
        pool = self._apool()
        return await self._aexecute(
            "GET",
            url,
            send=lambda: pool.stream(
                "GET", url, headers=self._headers(headers), params=params
            ),
            credentialed=_carries_credential(headers),
        )

    async def apost(
        self,
        url: str,
        *,
        body: str,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        """Async equivalent of ``post``."""
        pool = self._apool()
        return await self._aexecute(
            "POST",
            url,
            send=lambda: pool.stream(
                "POST",
                url,
                content=body,
                headers=self._headers(headers),
                follow_redirects=follow_redirects,
            ),
            credentialed=not follow_redirects or _carries_credential(headers),
        )

    def _pool(self) -> httpx.Client:
        if self._sync_pool is None:
            with self._pool_lock:
                if self._sync_pool is None:
                    self._sync_pool = httpx.Client(
                        timeout=self._timeout,
                        proxy=self._proxy,
                        transport=self._transport,
                        follow_redirects=True,
                    )
        return self._sync_pool

    def _apool(self) -> httpx.AsyncClient:
        if self._async_pool is None:
            with self._pool_lock:
                if self._async_pool is None:
                    self._async_pool = httpx.AsyncClient(
                        timeout=self._timeout,
                        proxy=self._proxy,
                        transport=self._transport,
                        follow_redirects=True,
                    )
        return self._async_pool

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        headers = {"User-Agent": self.USER_AGENT}
        if extra:
            headers.update(extra)
        return headers

    def _execute(
        self,
        method: str,
        url: str,
        *,
        send: Callable[[], AbstractContextManager[httpx.Response]],
        credentialed: bool,
    ) -> HttpResponse:
        """Run one request, retrying on this client's policy.

        ``send`` performs a single attempt and returns the raw httpx response; a
        failed exchange from it is treated as a transport failure. This is the
        sync twin of ``_aexecute`` below, kept separate rather than sharing a
        loop body through an async-agnostic abstraction, so neither path has to
        route through the other's concurrency model.
        """
        attempt = 0

        while True:
            if self._limiter is not None:
                self._limiter.acquire()
            _LOG.debug("%s %s (attempt %d)", method, url, attempt + 1)
            response, retry_after, permanent = self._attempt(send)
            self._record(response, retry_after, credentialed)

            if (
                not permanent
                and self._policy
                and self._policy.should_retry(attempt, response.status)
            ):
                delay = self._policy.get_delay(attempt, retry_after)
                self._log_retry(method, url, response.status, delay, attempt)
                time.sleep(delay)
                attempt += 1
                continue

            self._log_failure(method, url, response)
            return response

    async def _aexecute(
        self,
        method: str,
        url: str,
        *,
        send: Callable[[], AbstractAsyncContextManager[httpx.Response]],
        credentialed: bool,
    ) -> HttpResponse:
        """Async twin of ``_execute``: same policy, ``asyncio.sleep`` for backoff."""
        attempt = 0

        while True:
            if self._limiter is not None:
                await self._limiter.aacquire()
            _LOG.debug("%s %s (attempt %d)", method, url, attempt + 1)
            response, retry_after, permanent = await self._aattempt(send)
            self._record(response, retry_after, credentialed)

            if (
                not permanent
                and self._policy
                and self._policy.should_retry(attempt, response.status)
            ):
                delay = self._policy.get_delay(attempt, retry_after)
                self._log_retry(method, url, response.status, delay, attempt)
                await asyncio.sleep(delay)
                attempt += 1
                continue

            self._log_failure(method, url, response)
            return response

    def _record(
        self, response: HttpResponse, retry_after: str | None, credentialed: bool
    ) -> None:
        """Report one response to the limiter.

        Skipped for a transport failure (there is no response), and for a 403 on
        a credentialed request, which is a refused credential, not throttling.
        A credential travels in ``Authorization``, or in the body of a POST sent
        with ``follow_redirects=False``.
        """
        if self._limiter is None or response.transport_error is not None:
            return
        if response.status == 403 and credentialed:
            return
        self._limiter.record(response.status, retry_after_seconds(retry_after))

    def _attempt(
        self, send: Callable[[], AbstractContextManager[httpx.Response]]
    ) -> tuple[HttpResponse, str | None, bool]:
        """One exchange, plus the ``Retry-After`` it asked for, if any.

        The header is read here rather than carried on ``HttpResponse``: it is a
        retry input, spent before anything downstream sees the response.

        The deadline starts before the stream opens, so it covers connecting as
        well as reading. ``_BodyTooLarge`` and ``_AttemptTooSlow`` are permanent:
        the same server sends the same body at the same rate on a retry, so
        trying again only spends the backoff schedule to learn that.
        """
        deadline = time.monotonic() + self._max_duration
        try:
            with send() as raw:
                body = self._read(raw.iter_bytes(), raw, deadline)
                return (
                    HttpResponse(status=raw.status_code, body=body),
                    raw.headers.get("Retry-After"),
                    False,
                )
        except (_BodyTooLarge, _AttemptTooSlow) as exc:
            return self._to_error_response(exc), None, True
        except _PERMANENT_EXCEPTIONS as exc:
            return self._to_error_response(exc), None, True
        except _TRANSPORT_EXCEPTIONS as exc:
            return self._to_error_response(exc), None, False

    async def _aattempt(
        self, send: Callable[[], AbstractAsyncContextManager[httpx.Response]]
    ) -> tuple[HttpResponse, str | None, bool]:
        """Async twin of ``_attempt``."""
        deadline = time.monotonic() + self._max_duration
        try:
            async with send() as raw:
                body = await self._aread(raw, deadline)
                return (
                    HttpResponse(status=raw.status_code, body=body),
                    raw.headers.get("Retry-After"),
                    False,
                )
        except (_BodyTooLarge, _AttemptTooSlow) as exc:
            return self._to_error_response(exc), None, True
        except _PERMANENT_EXCEPTIONS as exc:
            return self._to_error_response(exc), None, True
        except _TRANSPORT_EXCEPTIONS as exc:
            return self._to_error_response(exc), None, False

    def _read(
        self, chunks: Iterable[bytes], raw: httpx.Response, deadline: float
    ) -> str:
        """Buffer a streamed body, refusing to exceed ``max_bytes`` or the deadline.

        Both limits are checked per chunk rather than from ``Content-Length``: a
        chunked response has no length to check, and a declared one is only the
        server's claim about what it is about to send.
        """
        buffered: list[bytes] = []
        total = 0
        for chunk in chunks:
            total += len(chunk)
            if total > self._max_bytes:
                raise _BodyTooLarge(
                    f"response body exceeded max_bytes={self._max_bytes}"
                )
            if time.monotonic() > deadline:
                raise _AttemptTooSlow(
                    f"attempt exceeded max_duration={self._max_duration}s "
                    f"after {total} bytes"
                )
            buffered.append(chunk)
        return b"".join(buffered).decode(
            raw.charset_encoding or "utf-8", errors="replace"
        )

    async def _aread(self, raw: httpx.Response, deadline: float) -> str:
        """Async twin of ``_read``. Duplicated for the same reason the walks are:
        an async iterator cannot be consumed by a sync loop."""
        buffered: list[bytes] = []
        total = 0
        async for chunk in raw.aiter_bytes():
            total += len(chunk)
            if total > self._max_bytes:
                raise _BodyTooLarge(
                    f"response body exceeded max_bytes={self._max_bytes}"
                )
            if time.monotonic() > deadline:
                raise _AttemptTooSlow(
                    f"attempt exceeded max_duration={self._max_duration}s "
                    f"after {total} bytes"
                )
            buffered.append(chunk)
        return b"".join(buffered).decode(
            raw.charset_encoding or "utf-8", errors="replace"
        )

    def _to_error_response(self, exc: Exception) -> HttpResponse:
        return HttpResponse(
            status=0, body="", transport_error=str(exc) or type(exc).__name__
        )

    def _log_retry(
        self, method: str, url: str, status: int, delay: float, attempt: int
    ) -> None:
        _LOG.warning(
            "%s %s returned status %d, retrying in %.1fs (attempt %d)",
            method,
            url,
            status,
            delay,
            attempt + 1,
        )

    def _log_failure(self, method: str, url: str, response: HttpResponse) -> None:
        if response.transport_error is not None:
            _LOG.warning("%s %s failed: %s", method, url, response.transport_error)
        elif response.status >= 400:
            _LOG.warning("%s %s failed with status %d", method, url, response.status)

    def close(self) -> None:
        """Release this client's sockets. Safe to call more than once.

        Both pools, not just the sync one: a client that served a ``search()``
        and an ``afetch()`` has two, and closing only the one named after the
        method leaks the other.

        Does not make the client unusable: the next request rebuilds the pool.
        """
        if self._sync_pool is not None:
            self._sync_pool.close()
            self._sync_pool = None
        self._close_async_pool_from_sync()

    async def aclose(self) -> None:
        """Async twin of ``close``. Releases both pools."""
        if self._async_pool is not None:
            await self._async_pool.aclose()
            self._async_pool = None
        if self._sync_pool is not None:
            self._sync_pool.close()
            self._sync_pool = None

    def _close_async_pool_from_sync(self) -> None:
        """Release the async pool from a synchronous ``close()``.

        ``httpx.AsyncClient`` has no synchronous close, so this needs a loop.
        With none running, one is started for the teardown. Inside a running loop
        that is not possible and the caller wanted ``await aclose()``, so it says
        so rather than leaking silently.
        """
        if self._async_pool is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._async_pool.aclose())
            self._async_pool = None
            return
        _LOG.warning(
            "close() cannot release the async pool from inside a running event "
            "loop; use `await aclose()`. The async pool is still open."
        )

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


def _carries_credential(headers: dict[str, str] | None) -> bool:
    """True if ``headers`` send an ``Authorization``, in any letter case."""
    return bool(headers) and any(
        name.lower() == "authorization" for name in headers or ()
    )
