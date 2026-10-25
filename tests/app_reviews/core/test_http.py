"""Tests for HttpClient: the pooled, injectable HTTP seam.

The module-level ``http_get``/``http_post`` helpers built a fresh
``httpx.Client`` per call, so an N-page walk paid N TLS handshakes, and every
provider carried a ``_transport: Any = None`` attribute that no production code
ever assigned purely so tests had somewhere to push a ``MockTransport``.
``HttpClient`` owns the pool and *is* that seam.
"""

import asyncio
import time
from unittest.mock import patch

import httpx
import pytest

from app_reviews.core.http import HttpClient
from app_reviews.models.config import RetryConfig


def _ok(_request):
    return httpx.Response(200, text="{}")


def _client(handler=_ok, **kwargs):
    return HttpClient(transport=httpx.MockTransport(handler), **kwargs)


async def _fake_asleep(_delay: float) -> None:
    return None


class TestConnectionReuse:
    def test_many_requests_share_one_pool(self):
        with patch("app_reviews.core.http.httpx.Client", wraps=httpx.Client) as spy:
            http = _client()
            for _ in range(5):
                http.get("https://example.test/x")

        assert spy.call_count == 1

    async def test_many_async_requests_share_one_pool(self):
        with patch(
            "app_reviews.core.http.httpx.AsyncClient", wraps=httpx.AsyncClient
        ) as spy:
            http = _client()
            for _ in range(5):
                await http.aget("https://example.test/x")

        assert spy.call_count == 1

    def test_a_sync_only_caller_never_builds_an_async_pool(self):
        with patch(
            "app_reviews.core.http.httpx.AsyncClient", wraps=httpx.AsyncClient
        ) as spy:
            _client().get("https://example.test/x")

        spy.assert_not_called()

    async def test_an_async_only_caller_never_builds_a_sync_pool(self):
        with patch("app_reviews.core.http.httpx.Client", wraps=httpx.Client) as spy:
            await _client().aget("https://example.test/x")

        spy.assert_not_called()


class TestGet:
    def test_returns_status_and_body(self):
        def handler(request):
            return httpx.Response(200, text="hello")

        resp = _client(handler).get("https://example.test/x")

        assert resp.status == 200
        assert resp.body == "hello"
        assert resp.transport_error is None

    def test_sends_params_and_headers_over_a_default_user_agent(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["ua"] = request.headers.get("user-agent")
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, text="{}")

        _client(handler).get(
            "https://example.test/x",
            params={"q": "1"},
            headers={"Authorization": "Bearer t"},
        )

        assert "q=1" in seen["url"]
        assert seen["ua"] == "app-reviews"
        assert seen["auth"] == "Bearer t"

    def test_a_failed_exchange_becomes_a_transport_error(self):
        def handler(request):
            raise httpx.ConnectError("no route to host")

        resp = _client(handler).get("https://example.test/x")

        assert resp.status == 0
        assert resp.transport_error is not None
        assert "no route to host" in resp.transport_error


class TestPost:
    def test_sends_the_body(self):
        seen = {}

        def handler(request):
            seen["body"] = request.content.decode()
            return httpx.Response(201, text="created")

        resp = _client(handler).post("https://example.test/x", body='{"a":1}')

        assert seen["body"] == '{"a":1}'
        assert resp.status == 201


class TestRetryIsOwnedByTheClient:
    """Retry config is HttpClient state, not a per-call argument."""

    def test_retries_a_retryable_status(self, monkeypatch):
        monkeypatch.setattr("app_reviews.core.http.time.sleep", lambda _d: None)
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(503, text="")

        resp = _client(handler, retry=RetryConfig(max_retries=2, backoff_factor=0)).get(
            "https://example.test/x"
        )

        assert len(attempts) == 3
        assert resp.status == 503

    async def test_async_retries_a_retryable_status(self, monkeypatch):
        monkeypatch.setattr("app_reviews.core.http.asyncio.sleep", _fake_asleep)
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(503, text="")

        await _client(handler, retry=RetryConfig(max_retries=2, backoff_factor=0)).aget(
            "https://example.test/x"
        )

        assert len(attempts) == 3

    def test_no_retry_config_means_one_attempt(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(503, text="")

        _client(handler).get("https://example.test/x")

        assert len(attempts) == 1


class TestLifecycle:
    def test_close_is_safe_before_any_request(self):
        _client().close()

    async def test_aclose_is_safe_before_any_request(self):
        await _client().aclose()

    def test_works_as_a_context_manager(self):
        with _client() as http:
            assert http.get("https://example.test/x").status == 200

    async def test_works_as_an_async_context_manager(self):
        async with _client() as http:
            resp = await http.aget("https://example.test/x")

        assert resp.status == 200

    def test_a_closed_pool_is_rebuilt_on_the_next_request(self):
        """close() releases sockets; it does not make the client unusable."""
        http = _client()
        http.get("https://example.test/x")
        http.close()

        assert http.get("https://example.test/x").status == 200


class TestAsyncParity:
    async def test_aget_matches_get(self):
        def handler(_request):
            return httpx.Response(200, text="hello")

        sync = _client(handler).get("https://example.test/x")
        asyncr = await _client(handler).aget("https://example.test/x")

        assert (sync.status, sync.body) == (asyncr.status, asyncr.body)

    async def test_apost_matches_post(self):
        def handler(request):
            return httpx.Response(201, text=request.content.decode())

        sync = _client(handler).post("https://example.test/x", body="payload")
        asyncr = await _client(handler).apost("https://example.test/x", body="payload")

        assert (sync.status, sync.body) == (asyncr.status, asyncr.body)

    async def test_async_transport_failure_captures_the_message(self):
        def handler(_request):
            raise httpx.ConnectError("connection refused")

        resp = await _client(handler).aget("https://example.test/x")

        assert resp.status == 0
        assert "connection refused" in (resp.transport_error or "")


class TestMalformedUrl:
    """``httpx.InvalidURL`` subclasses Exception, not ``httpx.HTTPError``, so it
    needs its own entry in ``_TRANSPORT_EXCEPTIONS``, or a stray tab or
    an unclosed bracket escapes as an exception instead of a status-0 response.
    No request is attempted, so these need no transport.
    """

    def test_get_returns_an_error_response_rather_than_raising(self):
        resp = HttpClient().get("http://[::1")

        assert resp.status == 0
        assert resp.transport_error
        assert resp.body == ""

    def test_post_returns_an_error_response_rather_than_raising(self):
        resp = HttpClient().post("http://[::1", body="x")

        assert resp.status == 0
        assert resp.transport_error

    async def test_aget_returns_an_error_response_rather_than_raising(self):
        resp = await HttpClient().aget("http://[::1")

        assert resp.status == 0
        assert resp.transport_error

    async def test_apost_returns_an_error_response_rather_than_raising(self):
        resp = await HttpClient().apost("http://[::1", body="x")

        assert resp.status == 0
        assert resp.transport_error


class TestConfiguration:
    def test_timeout_reaches_the_pool(self):
        http = _client(timeout=1.5)
        http.get("https://example.test/x")

        assert http._pool().timeout.read == pytest.approx(1.5)

    def test_proxy_is_accepted(self):
        """A proxy is threaded into the pool rather than each request."""
        http = HttpClient(proxy="http://proxy.test:8080")

        assert http._proxy == "http://proxy.test:8080"


class TestPoolConfigConflict:
    """``http=`` supplies a configured pool, so ``proxy=``/``retry=`` alongside it
    were silently discarded, so a caller could ask for ``max_retries=9`` and get a
    pool with no retry policy, with nothing said.
    """

    def _clients(self):
        from app_reviews import (
            AppStoreReviews,
            AppStoreSearch,
            GooglePlayReviews,
            GooglePlaySearch,
        )

        return (AppStoreReviews, GooglePlayReviews, AppStoreSearch, GooglePlaySearch)

    def test_http_with_retry_is_rejected(self):
        for cls in self._clients():
            with pytest.raises(TypeError, match="retry"):
                cls(http=HttpClient(), retry=RetryConfig(max_retries=9))

    def test_http_with_proxy_is_rejected(self):
        for cls in self._clients():
            with pytest.raises(TypeError, match="proxy"):
                cls(http=HttpClient(), proxy="http://proxy.test:8080")

    def test_the_error_names_both_arguments(self):
        from app_reviews import AppStoreReviews

        with pytest.raises(TypeError) as exc:
            AppStoreReviews(http=HttpClient(), proxy="http://p:8080")

        assert "http" in str(exc.value) and "proxy" in str(exc.value)

    def test_http_alone_is_fine(self):
        from app_reviews import AppStoreReviews

        pool = HttpClient()
        assert AppStoreReviews(http=pool)._http is pool

    def test_proxy_and_retry_without_http_are_fine(self):
        from app_reviews import AppStoreReviews

        retry = RetryConfig(max_retries=9)
        client = AppStoreReviews(proxy="http://p:8080", retry=retry)

        assert client._http._proxy == "http://p:8080"
        assert client._http._retry is retry


class TestCredentialsDoNotFollowRedirectsOffOrigin:
    """``follow_redirects`` is on, and credentialed requests set ``Authorization``
    by hand, so a redirect is a way a token could leave the host it was minted for.

    httpx strips the header on any origin change, which is why the pool can keep
    redirects enabled, because the iTunes endpoints need them. That protection
    lives in a pinned dependency, so it is asserted here rather than assumed: a
    bump that loosened it would otherwise leak a signed JWT silently.
    """

    HOST = "api.appstoreconnect.apple.com"

    def _final_hop(self, redirect_to: str):
        hops: list[tuple[str, str | None]] = []

        def handler(request):
            hops.append((str(request.url), request.headers.get("authorization")))
            if request.url.host == self.HOST and request.url.path == "/start":
                return httpx.Response(302, headers={"Location": redirect_to})
            return httpx.Response(200, text="{}")

        HttpClient(transport=httpx.MockTransport(handler)).get(
            f"https://{self.HOST}/start", headers={"Authorization": "Bearer JWT"}
        )
        return hops[-1]

    @pytest.mark.parametrize(
        "redirect_to",
        [
            "https://evil.example.com/collect",
            "https://api.appstoreconnect.apple.com.evil.com/collect",
            "https://other.apple.com/collect",
            "http://api.appstoreconnect.apple.com/downgraded",
            "https://api.appstoreconnect.apple.com:8443/other-port",
        ],
    )
    def test_the_token_is_dropped_when_the_origin_changes(self, redirect_to):
        url, auth = self._final_hop(redirect_to)

        assert url.startswith(redirect_to)
        assert auth is None

    def test_the_token_survives_a_same_origin_redirect(self):
        """Apple's own endpoints redirect; stripping here would break the fetch."""
        url, auth = self._final_hop(f"https://{self.HOST}/moved")

        assert url == f"https://{self.HOST}/moved"
        assert auth == "Bearer JWT"

    def test_redirects_stay_enabled(self):
        """The iTunes lookup and RSS endpoints rely on them."""
        assert HttpClient()._pool().follow_redirects is True


def _slept(handler, retry, monkeypatch, *, url="https://example.test/x"):
    """Every delay the client would have slept for, without sleeping."""
    delays: list[float] = []
    monkeypatch.setattr("app_reviews.core.http.time.sleep", delays.append)
    _client(handler, retry=retry).get(url)
    return delays


def _throttling(times: int, retry_after: str | None):
    """A handler that 429s ``times`` times, optionally with ``Retry-After``."""
    seen = {"n": 0}

    def handler(request):
        seen["n"] += 1
        if seen["n"] > times:
            return httpx.Response(200, text="ok")
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        return httpx.Response(429, headers=headers, text="slow down")

    return handler


class TestRetryAfterIsHonoured:
    """The server is the only party that knows when it will serve again.

    Retrying a 429 sooner than instructed is what turns throttling into a longer
    ban, so the header wins over the exponential schedule.
    """

    def test_a_delay_in_seconds_is_used(self, monkeypatch):
        delays = _slept(
            _throttling(1, "30"),
            RetryConfig(max_retries=3, max_backoff=60),
            monkeypatch,
        )

        assert delays == [30.0]

    def test_it_beats_the_exponential_schedule(self, monkeypatch):
        delays = _slept(
            _throttling(2, "5"),
            RetryConfig(max_retries=3, backoff_factor=0.5, max_backoff=60),
            monkeypatch,
        )

        assert delays == [5.0, 5.0]

    def test_an_absurd_delay_is_capped(self, monkeypatch):
        """A header cannot park a request for a day."""
        delays = _slept(
            _throttling(1, "86400"),
            RetryConfig(max_retries=3, max_backoff=60),
            monkeypatch,
        )

        assert delays == [60.0]

    @pytest.mark.parametrize("value", ["soon", "", "  ", "not-a-date", "nan", "NaN"])
    def test_an_unusable_value_falls_back_to_the_schedule(self, value, monkeypatch):
        delays = _slept(
            _throttling(1, value),
            RetryConfig(max_retries=3, backoff_factor=0.5, max_backoff=60),
            monkeypatch,
        )

        assert delays == [0.5]

    def test_a_negative_delay_means_now(self, monkeypatch):
        delays = _slept(
            _throttling(1, "-10"),
            RetryConfig(max_retries=3, max_backoff=60),
            monkeypatch,
        )

        assert delays == [0.0]

    def test_nan_does_not_read_as_now(self, monkeypatch):
        """``float("nan")`` parses, so it reached the clamp, and every
        comparison against NaN is False, so ``max(0.0, nan)`` is ``0.0``. A
        throttled client then retried with no backoff at all, which is the
        opposite of what a 429 asks for. Unlike ``-10`` it names no instant, so
        it is unusable rather than past."""
        delays = _slept(
            _throttling(3, "nan"),
            RetryConfig(max_retries=3, backoff_factor=0.5, max_backoff=60),
            monkeypatch,
        )

        assert delays == [0.5, 1.0, 2.0]

    def test_an_infinite_delay_is_still_capped(self, monkeypatch):
        """``inf`` names an instant, just an absurd one, so it caps like 86400
        rather than falling back. Waiting longer is the safe error for a 429."""
        delays = _slept(
            _throttling(1, "inf"),
            RetryConfig(max_retries=3, max_backoff=60),
            monkeypatch,
        )

        assert delays == [60.0]

    def test_an_http_date_is_accepted(self, monkeypatch):
        """RFC 9110 allows a date as well as a delay. A past one means "now"."""
        delays = _slept(
            _throttling(1, "Wed, 21 Oct 2015 07:28:00 GMT"),
            RetryConfig(max_retries=3, max_backoff=60),
            monkeypatch,
        )

        assert delays == [0.0]

    def test_no_header_uses_the_exponential_schedule(self, monkeypatch):
        delays = _slept(
            _throttling(2, None),
            RetryConfig(max_retries=3, backoff_factor=0.5, max_backoff=60),
            monkeypatch,
        )

        assert delays == [0.5, 1.0]


class TestBackoffIsCapped:
    """``backoff_factor * 2**attempt`` grows without bound.

    ``RetryConfig`` puts no ceiling on ``max_retries``, so an uncapped schedule
    let one request sit for days on its last attempt.
    """

    def test_the_schedule_stops_growing_at_max_backoff(self):
        from app_reviews.core.retry import RetryPolicy

        policy = RetryPolicy(RetryConfig(max_retries=20, backoff_factor=0.5))

        assert policy.get_delay(19) <= RetryConfig().max_backoff

    def test_a_long_schedule_never_exceeds_the_cap(self, monkeypatch):
        delays = _slept(
            _throttling(5, None),
            RetryConfig(max_retries=6, backoff_factor=10, max_backoff=25),
            monkeypatch,
        )

        assert delays == [10.0, 20.0, 25.0, 25.0, 25.0]


class TestConcurrentFirstUseBuildsOnePool:
    """``fetch`` fans out across threads on one shared client.

    Lazily building the pool is a check-then-set, so concurrent first use could
    build several and keep only the last, leaving the rest open with nothing
    holding a reference to close them.
    """

    def test_one_pool_survives_a_concurrent_first_request(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        built: list[httpx.Client] = []
        real_init = httpx.Client.__init__

        def slow_init(self, *args, **kwargs):
            built.append(self)
            time.sleep(0.01)  # widen the check-then-set window
            return real_init(self, *args, **kwargs)

        client = _client()
        barrier = threading.Barrier(8)

        def hit():
            barrier.wait()
            return client._pool()

        with (
            patch.object(httpx.Client, "__init__", slow_init),
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            pools = [f.result() for f in [pool.submit(hit) for _ in range(8)]]

        assert len(built) == 1
        assert len({id(p) for p in pools}) == 1

    async def test_one_async_pool_survives_concurrent_first_requests(self):
        import asyncio

        client = _client()
        pools = await asyncio.gather(
            *(asyncio.to_thread(client._apool) for _ in range(8))
        )

        assert len({id(p) for p in pools}) == 1


class TestACredentialBearingBodyDoesNotFollowRedirects:
    """httpx strips ``Authorization`` when the origin changes, which protects a
    header-borne token. A 307/308 re-sends the *body* verbatim, and the Google
    token exchange carries its signed assertion there.
    """

    def _hops(self, follow):
        hops = []

        def handler(request):
            hops.append((str(request.url), request.content.decode()))
            if "oauth2.googleapis.com" in str(request.url):
                return httpx.Response(
                    307, headers={"Location": "https://evil.example/collect"}
                )
            return httpx.Response(200, text="{}")

        _client(handler).post(
            "https://oauth2.googleapis.com/token",
            body="grant_type=x&assertion=SIGNED_ASSERTION",
            follow_redirects=follow,
        )
        return hops

    def test_the_assertion_does_not_reach_a_second_host(self):
        hops = self._hops(False)

        assert len(hops) == 1
        assert "evil.example" not in "".join(u for u, _ in hops)

    def test_following_would_have_leaked_it(self):
        """Pins why the flag matters, so nobody flips the default back."""
        hops = self._hops(True)

        assert any("evil.example" in u and "SIGNED_ASSERTION" in b for u, b in hops)

    async def test_the_async_post_agrees(self):
        hops = []

        async def run():
            def handler(request):
                hops.append(str(request.url))
                if "oauth2.googleapis.com" in str(request.url):
                    return httpx.Response(
                        307, headers={"Location": "https://evil.example/collect"}
                    )
                return httpx.Response(200, text="{}")

            await _client(handler).apost(
                "https://oauth2.googleapis.com/token",
                body="assertion=SIGNED_ASSERTION",
                follow_redirects=False,
            )

        await run()
        assert hops == ["https://oauth2.googleapis.com/token"]


class TestAPermanentlyInvalidUrlIsNotRetried:
    """``status == 0`` means "the exchange never completed", which is normally
    worth retrying. A malformed URL never completes either, but it will never
    complete on the tenth attempt either, so the backoff is pure delay."""

    def test_it_fails_immediately(self):
        import time

        started = time.monotonic()
        response = _client(retry=RetryConfig(max_retries=3, backoff_factor=0.2)).get(
            "http://[::1"
        )

        assert response.status == 0
        assert time.monotonic() - started < 0.1

    def test_a_real_transport_failure_is_still_retried(self, monkeypatch):
        monkeypatch.setattr("app_reviews.core.http.time.sleep", lambda _d: None)
        attempts = []

        def handler(request):
            attempts.append(1)
            raise httpx.ConnectError("refused")

        _client(handler, retry=RetryConfig(max_retries=2, backoff_factor=0)).get(
            "https://example.test/x"
        )

        assert len(attempts) == 3


class TestClosingReleasesBothPools:
    """A client that served both ladders leaked whichever pool the caller did
    not close by name."""

    def test_close_releases_the_async_pool_too(self):
        """Called with no loop running: the common case for a script that mixed
        a sync ``search()`` with an ``asyncio.run(...)`` fetch."""
        import asyncio

        client = _client()
        client.get("https://example.test/x")
        asyncio.run(client.aget("https://example.test/x"))
        assert client._async_pool is not None

        client.close()

        assert client._sync_pool is None
        assert client._async_pool is None

    async def test_aclose_releases_the_sync_pool_too(self):
        client = _client()
        client.get("https://example.test/x")
        await client.aget("https://example.test/x")

        await client.aclose()

        assert client._sync_pool is None
        assert client._async_pool is None

    async def test_close_inside_a_running_loop_says_it_cannot(self, caplog):
        """``httpx.AsyncClient`` has no synchronous close, so from inside a loop
        the only honest options are to say so or to leak quietly."""
        import logging

        client = _client()
        await client.aget("https://example.test/x")

        with caplog.at_level(logging.WARNING):
            client.close()

        assert "await aclose()" in caplog.text
        assert client._async_pool is not None


class _TrickleStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """Serves a body one chunk at a time, pausing between chunks.

    Models the case a per-socket-read timeout cannot catch: every individual read
    lands well inside the deadline, so the exchange never times out, while the
    body keeps growing and the worker keeps waiting.
    """

    def __init__(self, chunk: bytes, count: int, pause: float):
        self._chunk = chunk
        self._count = count
        self._pause = pause

    def __iter__(self):
        for _ in range(self._count):
            time.sleep(self._pause)
            yield self._chunk

    async def __aiter__(self):
        for _ in range(self._count):
            await asyncio.sleep(self._pause)
            yield self._chunk


class TestResponseBodyIsCapped:
    """A body is read into memory, so its size cannot be the server's choice.

    ``raw.text`` on a non-streaming request has already spent the memory by the
    time it returns, so the cap has to be enforced while the body arrives.
    """

    def test_a_body_within_the_cap_is_returned_whole(self):
        body = "x" * 1000

        response = _client(
            lambda _r: httpx.Response(200, text=body), max_bytes=1000
        ).get("https://example.test/x")

        assert response.status == 200
        assert response.body == body

    def test_a_body_over_the_cap_fails_instead_of_being_read(self):
        response = _client(
            lambda _r: httpx.Response(200, content=b"x" * 5000), max_bytes=1000
        ).get("https://example.test/x")

        assert response.status == 0
        assert response.transport_error is not None
        assert "1000" in response.transport_error

    def test_an_oversized_body_is_not_retried(self):
        """It will be the same size on the next attempt; retrying spends the
        whole backoff schedule to learn that."""
        calls = []

        def handler(_request):
            calls.append(1)
            return httpx.Response(200, content=b"x" * 5000)

        _client(handler, max_bytes=1000, retry=RetryConfig(max_retries=3)).get(
            "https://example.test/x"
        )

        assert len(calls) == 1

    async def test_the_async_path_caps_too(self):
        response = await _client(
            lambda _r: httpx.Response(200, content=b"x" * 5000), max_bytes=1000
        ).aget("https://example.test/x")

        assert response.status == 0
        assert "1000" in (response.transport_error or "")

    def test_the_cap_does_not_disturb_a_normal_walk(self):
        """Default cap is far above any real page, so nothing changes."""
        response = _client(lambda _r: httpx.Response(200, text='{"a": 1}')).get(
            "https://example.test/x"
        )

        assert response.json() == {"a": 1}


class TestAttemptHasAWallClockDeadline:
    """httpx timeouts are per socket operation, not per exchange.

    A server that answers every read inside the read timeout can still hold a
    worker forever, so one attempt gets an overall budget of its own.
    """

    def test_a_trickled_body_is_abandoned(self):
        response = _client(
            lambda _r: httpx.Response(200, stream=_TrickleStream(b"x", 20, 0.02)),
            max_duration=0.05,
        ).get("https://example.test/x")

        assert response.status == 0
        assert "0.05" in (response.transport_error or "")

    def test_a_prompt_response_is_unaffected(self):
        response = _client(
            lambda _r: httpx.Response(200, text="{}"), max_duration=5.0
        ).get("https://example.test/x")

        assert response.status == 200

    async def test_the_async_path_has_a_deadline_too(self):
        response = await _client(
            lambda _r: httpx.Response(200, stream=_TrickleStream(b"x", 20, 0.02)),
            max_duration=0.05,
        ).aget("https://example.test/x")

        assert response.status == 0
        assert "0.05" in (response.transport_error or "")

    def test_a_late_empty_response_is_abandoned(self):
        """No body chunk arrives to check against, so the deadline must also be
        checked once the exchange completes."""

        def late_and_empty(_request):
            time.sleep(0.1)
            return httpx.Response(200, content=b"")

        response = _client(late_and_empty, max_duration=0.05).get(
            "https://example.test/x"
        )

        assert response.status == 0
        assert "max_duration=0.05" in (response.transport_error or "")

    def test_an_overrun_attempt_is_not_retried(self):
        attempts: list[int] = []

        def late_and_empty(_request):
            attempts.append(1)
            time.sleep(0.1)
            return httpx.Response(200, content=b"")

        _client(
            late_and_empty,
            max_duration=0.05,
            retry=RetryConfig(max_retries=2, backoff_factor=0),
        ).get("https://example.test/x")

        assert len(attempts) == 1

    def test_socket_waits_are_capped_by_the_attempt_budget(self):
        """A connect or read wait longer than the whole attempt's budget could
        only ever end in an overrun, so httpx is never asked to wait that long."""
        timeouts: dict[str, float] = {}

        def handler(request):
            timeouts.update(request.extensions["timeout"])
            return httpx.Response(200)

        _client(handler, timeout=30.0, max_duration=5.0).get("https://example.test/x")

        assert timeouts == {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}

    async def test_the_async_path_abandons_a_stalled_exchange(self):
        async def stalled(_request):
            await asyncio.sleep(5)
            return httpx.Response(200)

        started = time.monotonic()
        response = await _client(stalled, max_duration=0.05).aget(
            "https://example.test/x"
        )

        assert time.monotonic() - started < 1
        assert response.status == 0
        assert "max_duration=0.05" in (response.transport_error or "")

    async def test_an_outside_cancellation_still_propagates(self):
        async def stalled(_request):
            await asyncio.sleep(5)
            return httpx.Response(200)

        request = asyncio.create_task(
            _client(stalled, max_duration=10.0).aget("https://example.test/x")
        )
        await asyncio.sleep(0.01)
        request.cancel()

        with pytest.raises(asyncio.CancelledError):
            await request
