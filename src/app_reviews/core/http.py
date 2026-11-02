"""httpx-based request execution with retry and proxy support."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from app_reviews.core import safe_json
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

DEFAULT_TIMEOUT = 30.0
"""Seconds per socket operation when neither ``timeout`` nor a ``RetryConfig``
names one."""

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

Async attempts are cancelled at the deadline. A sync attempt cannot be
interrupted mid-read, so it is rejected once it completes past the deadline,
and each socket wait is capped at this budget; one attempt can therefore run
up to a few socket waits over it before it is abandoned.
"""


class _BodyTooLarge(Exception):
    """A response body exceeded ``max_bytes``. Permanent: a retry re-reads it."""


class _AttemptTooSlow(Exception):
    """An attempt outlived ``max_duration``. Permanent: retrying re-waits it."""


_ATTEMPT_FAILURES = (_BodyTooLarge, _AttemptTooSlow, *_TRANSPORT_EXCEPTIONS)
"""Everything one attempt can fail with and still return an ``HttpResponse``."""

_PERMANENT_ATTEMPT_FAILURES = (_BodyTooLarge, _AttemptTooSlow, *_PERMANENT_EXCEPTIONS)
"""The ``_ATTEMPT_FAILURES`` a retry cannot fix.

``_BodyTooLarge`` and ``_AttemptTooSlow`` are permanent: the same server sends
the same body at the same rate on a retry, so trying again only spends the
backoff schedule to learn that. Every other transport failure may be transient.
"""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """The outcome of one HTTP exchange.

    ``status`` is 0 and ``transport_error`` is set when the exchange never
    completed, such as a connection failure or timeout. In that case ``body`` is
    empty and ``transport_error`` holds the real exception text.

    ``retry_after`` is the raw ``Retry-After`` header of the final attempt, so a
    caller that gives up on a 429 can say how long the store asked it to wait.

    ``body`` is left out of ``repr``: some responses (the Google token exchange)
    carry a bearer token in it.
    """

    status: int
    body: str = field(repr=False)
    transport_error: str | None = None
    retry_after: str | None = None

    @property
    def ok(self) -> bool:
        """True for a 2xx response."""
        return 200 <= self.status < 300

    def json(self) -> Any:
        """Parse body as JSON."""
        return safe_json.loads(self.body)


@dataclass(frozen=True, slots=True)
class _Request:
    """One request, fully decided: what to send and how to treat the outcome.

    Built once per public call by ``HttpClient._build_request`` and read by every
    attempt, so retries and logging never re-derive anything from loose arguments.
    """

    method: str
    url: str
    headers: dict[str, str]
    body: str | None
    params: dict[str, str] | None
    follow_redirects: bool
    is_retryable: bool
    is_credentialed: bool
    """True when a 403 means a refused credential rather than throttling."""

    def to_stream_arguments(self) -> dict[str, Any]:
        """The keyword arguments for ``httpx`` ``stream``."""
        return {
            "method": self.method,
            "url": self.url,
            "headers": self.headers,
            "content": self.body,
            "params": self.params,
            "follow_redirects": self.follow_redirects,
        }


class HttpClient:
    """Owns one connection pool, and is the seam tests inject through.

    Two jobs. Connection reuse: pooled connections can be reused across pages
    when the server permits it. And the injection seam:
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
        timeout: float | None = None,
        proxy: str | None = None,
        retry: RetryConfig | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_duration: float = DEFAULT_MAX_DURATION,
        rate_limiter: RequestLimiter | None = None,
    ) -> None:
        timeout = self._resolve_timeout(timeout, retry)
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_duration = max_duration
        # A socket wait longer than the whole attempt's budget can only end in an
        # overrun, so no single wait is allowed to exceed it.
        self._socket_timeout = min(timeout, max_duration)
        self._proxy = proxy
        self._retry = retry
        self._policy = RetryPolicy(retry) if retry else None
        self._limiter = rate_limiter
        self._transport = transport
        self._sync_pool: httpx.Client | None = None
        self._async_pool: httpx.AsyncClient | None = None
        self._async_pool_loop: asyncio.AbstractEventLoop | None = None
        self._pool_lock = threading.Lock()

    @staticmethod
    def _resolve_timeout(timeout: float | None, retry: RetryConfig | None) -> float:
        """An explicit ``timeout`` wins, then ``retry.timeout``, then the default.

        ``RetryConfig`` documents ``timeout`` as the per-request timeout, so a
        client built from one honours it without the caller saying it twice.
        """
        if timeout is not None:
            return timeout
        if retry is not None:
            return retry.timeout
        return DEFAULT_TIMEOUT

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> HttpResponse:
        """Perform a GET on the shared pool."""
        return self._execute(
            self._build_request("GET", url, headers=headers, params=params)
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
        return self._execute(
            self._build_request(
                "POST",
                url,
                headers=headers,
                body=body,
                follow_redirects=follow_redirects,
                has_credential_in_body=not follow_redirects,
            )
        )

    def send_once(
        self,
        method: str,
        url: str,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        """Send one attempt, never retried and never redirected, for a write.

        A retry, or a followed 307/308 (which re-sends the body), could apply
        the write twice. The one attempt's outcome is returned, whatever it was.
        """
        return self._execute(self._build_write_request(method, url, body, headers))

    async def aget(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> HttpResponse:
        """Async equivalent of ``get``."""
        return await self._aexecute(
            self._build_request("GET", url, headers=headers, params=params)
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
        return await self._aexecute(
            self._build_request(
                "POST",
                url,
                headers=headers,
                body=body,
                follow_redirects=follow_redirects,
                has_credential_in_body=not follow_redirects,
            )
        )

    async def asend_once(
        self,
        method: str,
        url: str,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        """Async equivalent of ``send_once``."""
        return await self._aexecute(
            self._build_write_request(method, url, body, headers)
        )

    def _build_write_request(
        self, method: str, url: str, body: str | None, headers: dict[str, str] | None
    ) -> _Request:
        """The spec of a write: one attempt, no redirects, no retry."""
        return self._build_request(
            method,
            url,
            headers=headers,
            body=body,
            follow_redirects=False,
            is_retryable=False,
        )

    def _pool(self) -> httpx.Client:
        if self._sync_pool is None:
            with self._pool_lock:
                if self._sync_pool is None:
                    self._sync_pool = httpx.Client(
                        timeout=self._socket_timeout,
                        proxy=self._proxy,
                        transport=cast("httpx.BaseTransport | None", self._transport),
                        follow_redirects=True,
                    )
        return self._sync_pool

    def _apool(self) -> httpx.AsyncClient:
        """The async pool for the running loop, rebuilt when the loop changed.

        Pooled connections belong to the loop that opened them, so a pool built
        under a loop that has since closed (a second ``asyncio.run``) fails every
        request. The stale pool cannot be closed from here, its loop is gone, so
        it is dropped and a new one built under the same lock as first use. A
        client serves one event loop at a time.
        """
        loop = _running_loop()
        pool = self._async_pool_for(loop)
        if pool is not None:
            return pool
        with self._pool_lock:
            pool = self._async_pool_for(loop)
            if pool is None:
                pool = httpx.AsyncClient(
                    timeout=self._socket_timeout,
                    proxy=self._proxy,
                    transport=cast("httpx.AsyncBaseTransport | None", self._transport),
                    follow_redirects=True,
                )
                self._async_pool = pool
                self._async_pool_loop = loop
        return pool

    def _async_pool_for(
        self, loop: asyncio.AbstractEventLoop | None
    ) -> httpx.AsyncClient | None:
        """The current async pool if it was built under ``loop``, else None."""
        if self._async_pool_loop is not loop:
            return None
        return self._async_pool

    def _build_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        body: str | None = None,
        params: dict[str, str] | None = None,
        follow_redirects: bool = True,
        is_retryable: bool = True,
        has_credential_in_body: bool = False,
    ) -> _Request:
        """The one place a call's redirect and credential policy is decided.

        A request is credentialed when it sends ``Authorization`` or, per
        ``has_credential_in_body``, carries a credential in its body (the caller
        then also refuses redirects, since a 307/308 would re-send the body).
        """
        return _Request(
            method=method,
            url=url,
            headers={"User-Agent": self.USER_AGENT, **(headers or {})},
            body=body,
            params=params,
            follow_redirects=follow_redirects,
            is_retryable=is_retryable,
            is_credentialed=has_credential_in_body or _carries_credential(headers),
        )

    def _execute(self, request: _Request) -> HttpResponse:
        """Run one request, retrying on this client's policy.

        A failed exchange is treated as a transport failure. This is the sync
        twin of ``_aexecute`` below, kept separate rather than sharing a loop
        body through an async-agnostic abstraction, so neither path has to route
        through the other's concurrency model.
        """
        pool = self._pool()
        attempt = 0

        while True:
            if self._limiter is not None:
                self._limiter.acquire()
            _LOG.debug("%s %s (attempt %d)", request.method, request.url, attempt + 1)
            response, is_permanent = self._attempt(pool, request)
            self._record(response, request.is_credentialed)

            delay = self._compute_retry_delay(
                attempt,
                response,
                is_permanent=is_permanent,
                is_retryable=request.is_retryable,
            )
            if delay is None:
                self._log_failure(request, response)
                return response

            self._log_retry(request, response.status, delay, attempt)
            time.sleep(delay)
            attempt += 1

    async def _aexecute(self, request: _Request) -> HttpResponse:
        """Async twin of ``_execute``: same policy, ``asyncio.sleep`` for backoff."""
        pool = self._apool()
        attempt = 0

        while True:
            if self._limiter is not None:
                await self._limiter.aacquire()
            _LOG.debug("%s %s (attempt %d)", request.method, request.url, attempt + 1)
            response, is_permanent = await self._aattempt(pool, request)
            self._record(response, request.is_credentialed)

            delay = self._compute_retry_delay(
                attempt,
                response,
                is_permanent=is_permanent,
                is_retryable=request.is_retryable,
            )
            if delay is None:
                self._log_failure(request, response)
                return response

            self._log_retry(request, response.status, delay, attempt)
            await asyncio.sleep(delay)
            attempt += 1

    def _compute_retry_delay(
        self,
        attempt: int,
        response: HttpResponse,
        *,
        is_permanent: bool,
        is_retryable: bool,
    ) -> float | None:
        """Seconds to wait before the next attempt, or None if this one is final."""
        if not is_retryable or is_permanent or not self._policy:
            return None
        if not self._policy.should_retry(attempt, response.status):
            return None
        return self._policy.get_delay(attempt, response.retry_after)

    def _record(self, response: HttpResponse, is_credentialed: bool) -> None:
        """Report one response to the limiter.

        Skipped for a transport failure (there is no response), and for a 403 on
        a credentialed request, which is a refused credential, not throttling.
        A credential travels in ``Authorization``, or in the body of a POST sent
        with ``follow_redirects=False``.
        """
        if self._limiter is None or response.transport_error is not None:
            return
        if response.status == 403 and is_credentialed:
            return
        self._limiter.record(response.status, retry_after_seconds(response.retry_after))

    def _attempt(
        self, pool: httpx.Client, request: _Request
    ) -> tuple[HttpResponse, bool]:
        """One exchange, and whether its failure is permanent.

        The deadline starts before the stream opens, so it covers connecting as
        well as reading.
        """
        deadline = time.monotonic() + self._max_duration
        try:
            with pool.stream(**request.to_stream_arguments()) as raw:
                body = self._read(raw.iter_bytes(), raw, deadline)
                return self._to_response(raw, body), False
        except _ATTEMPT_FAILURES as exc:
            return self._classify_failure(exc)

    async def _aattempt(
        self, pool: httpx.AsyncClient, request: _Request
    ) -> tuple[HttpResponse, bool]:
        """Async twin of ``_attempt``, also cancelled the moment the deadline
        passes rather than only checked between body chunks."""
        deadline = time.monotonic() + self._max_duration
        try:
            async with (
                asyncio.timeout(self._max_duration),
                pool.stream(**request.to_stream_arguments()) as raw,
            ):
                body = await self._aread(raw, deadline)
                return self._to_response(raw, body), False
        except TimeoutError:
            overrun = _AttemptTooSlow(
                f"attempt exceeded max_duration={self._max_duration}s"
            )
            return self._classify_failure(overrun)
        except _ATTEMPT_FAILURES as exc:
            return self._classify_failure(exc)

    def _classify_failure(self, exc: Exception) -> tuple[HttpResponse, bool]:
        """The error response for a failed attempt, and whether it is permanent
        (one of ``_PERMANENT_ATTEMPT_FAILURES``)."""
        is_permanent = isinstance(exc, _PERMANENT_ATTEMPT_FAILURES)
        return self._to_error_response(exc), is_permanent

    def _to_response(self, raw: httpx.Response, body: str) -> HttpResponse:
        return HttpResponse(
            status=raw.status_code,
            body=body,
            retry_after=raw.headers.get("Retry-After"),
        )

    def _read(
        self, chunks: Iterable[bytes], raw: httpx.Response, deadline: float
    ) -> str:
        """Buffer a streamed body, refusing to exceed ``max_bytes`` or the deadline.

        Both limits are checked per chunk rather than from ``Content-Length``: a
        chunked response has no length to check, and a declared one is only the
        server's claim about what it is about to send. The deadline is checked
        once more at the end, for a response that arrived late with no body.
        """
        buffered: list[bytes] = []
        total_bytes = 0
        for chunk in chunks:
            total_bytes += len(chunk)
            self._check_body_size(total_bytes)
            self._check_deadline(deadline, total_bytes)
            buffered.append(chunk)
        self._check_deadline(deadline, total_bytes)
        return self._decode_body(buffered, raw)

    async def _aread(self, raw: httpx.Response, deadline: float) -> str:
        """Async twin of ``_read``. Duplicated for the same reason the walks are:
        an async iterator cannot be consumed by a sync loop."""
        buffered: list[bytes] = []
        total_bytes = 0
        async for chunk in raw.aiter_bytes():
            total_bytes += len(chunk)
            self._check_body_size(total_bytes)
            self._check_deadline(deadline, total_bytes)
            buffered.append(chunk)
        self._check_deadline(deadline, total_bytes)
        return self._decode_body(buffered, raw)

    def _check_body_size(self, total_bytes: int) -> None:
        if total_bytes > self._max_bytes:
            raise _BodyTooLarge(f"response body exceeded max_bytes={self._max_bytes}")

    def _check_deadline(self, deadline: float, total_bytes: int) -> None:
        if time.monotonic() > deadline:
            raise _AttemptTooSlow(
                f"attempt exceeded max_duration={self._max_duration}s "
                f"after {total_bytes} bytes"
            )

    def _decode_body(self, buffered: list[bytes], raw: httpx.Response) -> str:
        """Decode with the charset the server named, or UTF-8 when it names none.

        A charset Python has no codec for (``charset=bogus``) is the server's
        quirk, not a failed exchange, and would otherwise escape as a
        ``LookupError`` from a client that promises ``HttpResponse``.
        """
        body = b"".join(buffered)
        try:
            return body.decode(raw.charset_encoding or "utf-8", errors="replace")
        except LookupError:
            return body.decode("utf-8", errors="replace")

    def _to_error_response(self, exc: Exception) -> HttpResponse:
        return HttpResponse(
            status=0, body="", transport_error=str(exc) or type(exc).__name__
        )

    def _log_retry(
        self, request: _Request, status: int, delay: float, attempt: int
    ) -> None:
        _LOG.warning(
            "%s %s returned status %d, retrying in %.1fs (attempt %d)",
            request.method,
            request.url,
            status,
            delay,
            attempt + 1,
        )

    def _log_failure(self, request: _Request, response: HttpResponse) -> None:
        method, url = request.method, request.url
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
        if self._is_async_pool_orphaned():
            self._forget_async_pool()
        elif self._async_pool is not None:
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
        if self._is_async_pool_orphaned():
            self._forget_async_pool()
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

    def _is_async_pool_orphaned(self) -> bool:
        """Whether the async pool was built under an event loop that has closed.

        Its connections belong to that loop, so ``aclose()`` on them schedules
        callbacks there and raises ``Event loop is closed``: the usual ending of a
        script that ran ``asyncio.run(client.afetch(...))`` and then ``close()``.
        Nothing can be closed through a dead loop; the sockets go when the pool is
        garbage collected.
        """
        loop = self._async_pool_loop
        return self._async_pool is not None and loop is not None and loop.is_closed()

    def _forget_async_pool(self) -> None:
        self._async_pool = None
        self._async_pool_loop = None

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """The running event loop, or None when called outside one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _carries_credential(headers: dict[str, str] | None) -> bool:
    """True if ``headers`` send an ``Authorization``, in any letter case."""
    return bool(headers) and any(
        name.lower() == "authorization" for name in headers or ()
    )
