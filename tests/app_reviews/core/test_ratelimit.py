"""Tests for RateLimiter and how HttpClient and the clients use it."""

import asyncio
import contextlib
import threading
from itertools import pairwise

import httpx
import pytest

from app_reviews import (
    AppReviewsError,
    AppStoreReviews,
    AppStoreSearch,
    GooglePlayReviews,
    GooglePlaySearch,
    HttpClient,
    RateLimiter,
    RetryConfig,
)


class _Clock:
    """Stands in for the limiter's clock and sleeps: sleeping advances time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    async def asleep(self, seconds: float) -> None:
        self.sleep(seconds)

    def waited(self) -> float:
        """Total slept since the last call."""
        total = sum(self.slept)
        self.slept.clear()
        return total


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr("app_reviews.core.ratelimit.monotonic", fake.monotonic)
    monkeypatch.setattr("app_reviews.core.ratelimit.sleep", fake.sleep)
    monkeypatch.setattr("app_reviews.core.ratelimit.asleep", fake.asleep)
    monkeypatch.setattr("app_reviews.core.http.time.sleep", lambda _d: None)
    return fake


def _http(handler, limiter, **kwargs):
    return HttpClient(
        transport=httpx.MockTransport(handler), rate_limiter=limiter, **kwargs
    )


def _answer(*statuses, headers=None):
    """A handler answering ``statuses`` in order, then 200 forever."""
    queue = list(statuses)

    def handler(request):
        status = queue.pop(0) if queue else 200
        return httpx.Response(status, text="", headers=headers or {})

    return handler


class TestTokenBucket:
    def test_burst_passes_at_once_then_paces_at_rate(self, clock):
        limiter = RateLimiter(rate=2, burst=3)

        for _ in range(3):
            limiter.acquire()
        assert clock.waited() == 0

        limiter.acquire()
        assert clock.waited() == pytest.approx(0.5)

    def test_idle_time_refills_only_up_to_burst(self, clock):
        limiter = RateLimiter(rate=1, burst=2)
        limiter.acquire()
        limiter.acquire()

        clock.now += 100
        limiter.acquire()
        limiter.acquire()
        assert clock.waited() == 0

        limiter.acquire()
        assert clock.waited() == pytest.approx(1.0)

    async def test_sync_and_async_holders_draw_from_one_bucket(self, clock):
        limiter = RateLimiter(rate=1)
        limiter.acquire()

        await limiter.aacquire()

        assert clock.waited() == pytest.approx(1.0)

    async def test_concurrent_tasks_are_spaced_by_the_rate(self, clock):
        limiter = RateLimiter(rate=1)
        granted: list[float] = []

        async def take() -> None:
            await limiter.aacquire()
            granted.append(clock.now)

        await asyncio.gather(*(take() for _ in range(5)))

        gaps = [later - earlier for earlier, later in pairwise(granted)]
        assert len(granted) == 5
        assert all(gap >= 1.0 - 1e-9 for gap in gaps)

    def test_threads_never_take_more_tokens_than_the_bucket_holds(self, monkeypatch):
        """50 threads race for 10 tokens; a sleep means the caller had to wait."""

        class _WouldWait(Exception):
            pass

        def refuse(_seconds: float) -> None:
            raise _WouldWait

        monkeypatch.setattr("app_reviews.core.ratelimit.sleep", refuse)
        limiter = RateLimiter(rate=1e-6, burst=10)
        start = threading.Barrier(50)
        granted: list[bool] = []

        def take() -> None:
            start.wait()
            try:
                limiter.acquire()
            except _WouldWait:
                granted.append(False)
            else:
                granted.append(True)

        threads = [threading.Thread(target=take) for _ in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert granted.count(True) == 10
        assert granted.count(False) == 40


class TestPenalize:
    def test_pause_holds_every_token_until_it_ends(self, clock):
        limiter = RateLimiter(rate=100, burst=100)

        limiter.penalize(10)
        limiter.acquire()

        assert clock.waited() == pytest.approx(10.0)

    def test_a_shorter_penalty_never_shortens_a_longer_pause(self, clock):
        limiter = RateLimiter(rate=100, burst=100)

        limiter.penalize(60)
        limiter.penalize(5)
        limiter.acquire()

        assert clock.waited() == pytest.approx(60.0)

    def test_a_later_penalty_extends_the_pause_from_now(self, clock):
        limiter = RateLimiter(rate=100, burst=100)
        limiter.penalize(5)
        clock.now += 2

        limiter.penalize(10)
        limiter.acquire()

        assert clock.waited() == pytest.approx(10.0)

    async def test_async_holders_wait_out_the_pause(self, clock):
        limiter = RateLimiter(rate=100, burst=100)

        limiter.penalize(7)
        await limiter.aacquire()

        assert clock.waited() == pytest.approx(7.0)


class TestValidation:
    @pytest.mark.parametrize(
        ("kwargs", "error"),
        [
            ({"rate": 0}, ValueError),
            ({"rate": -1}, ValueError),
            ({"rate": float("nan")}, ValueError),
            ({"rate": float("inf")}, ValueError),
            ({"rate": True}, TypeError),
            ({"rate": "1"}, TypeError),
            ({"rate": 1, "burst": 0}, ValueError),
            ({"rate": 1, "burst": 1.5}, TypeError),
            ({"rate": 1, "burst": True}, TypeError),
            ({"rate": 1, "initial_penalty": 0}, ValueError),
            ({"rate": 1, "max_penalty": float("inf")}, ValueError),
            ({"rate": 1, "initial_penalty": 60, "max_penalty": 30}, ValueError),
        ],
    )
    def test_rejects_unusable_settings(self, kwargs, error):
        with pytest.raises(error):
            RateLimiter(**kwargs)

    @pytest.mark.parametrize(
        ("seconds", "error"),
        [(-1, ValueError), (float("nan"), ValueError), (float("inf"), ValueError)],
    )
    def test_penalize_rejects_unusable_seconds(self, seconds, error):
        with pytest.raises(error):
            RateLimiter(rate=1).penalize(seconds)


class TestHttpClientWithLimiter:
    def test_every_attempt_takes_a_token_including_retries(self, clock):
        limiter = RateLimiter(rate=1)
        http = _http(
            _answer(503, 503),
            limiter,
            retry=RetryConfig(max_retries=2, retry_on=(503,)),
        )

        response = http.get("https://example.test/x")

        assert response.status == 200
        assert clock.slept == pytest.approx([1.0, 1.0])

    @pytest.mark.parametrize("status", [429, 403])
    def test_throttled_answer_pauses_every_client_sharing_the_limiter(
        self, clock, status
    ):
        limiter = RateLimiter(rate=100, burst=100)
        throttled = _http(_answer(status), limiter)
        sibling = _http(_answer(), limiter)

        throttled.get("https://example.test/x")
        sibling.get("https://example.test/y")

        assert clock.waited() == pytest.approx(30.0)

    def test_retry_after_sets_the_pause(self, clock):
        limiter = RateLimiter(rate=100, burst=100)
        http = _http(_answer(429, headers={"Retry-After": "120"}), limiter)

        http.get("https://example.test/x")
        http.get("https://example.test/x")

        assert clock.waited() == pytest.approx(120.0)

    def test_retry_after_is_capped_at_max_penalty(self, clock):
        limiter = RateLimiter(rate=100, burst=100, max_penalty=300)
        http = _http(_answer(429, headers={"Retry-After": "86400"}), limiter)

        http.get("https://example.test/x")
        http.get("https://example.test/x")

        assert clock.waited() == pytest.approx(300.0)

    def test_pause_doubles_per_consecutive_throttle_and_success_resets_it(self, clock):
        limiter = RateLimiter(rate=100, burst=100, initial_penalty=30, max_penalty=100)
        http = _http(_answer(403, 403, 403, 200, 403), limiter)
        waits = []

        for _ in range(6):
            http.get("https://example.test/x")
            waits.append(clock.waited())

        # Each wait is paid by the request after the throttled one.
        assert waits == pytest.approx([0, 30, 60, 100, 0, 30])

    def test_penalty_ignores_the_retry_configs_max_backoff(self, clock):
        limiter = RateLimiter(rate=100, burst=100)
        http = _http(
            _answer(403), limiter, retry=RetryConfig(max_backoff=5, retry_on=(503,))
        )

        http.get("https://example.test/x")
        http.get("https://example.test/x")

        assert clock.waited() == pytest.approx(30.0)

    @pytest.mark.parametrize(
        "request_with_credential",
        [
            lambda http: http.get(
                "https://example.test/x", headers={"Authorization": "Bearer t"}
            ),
            lambda http: http.get(
                "https://example.test/x", headers={"authorization": "Bearer t"}
            ),
            lambda http: http.post(
                "https://example.test/token", body="assertion=t", follow_redirects=False
            ),
        ],
    )
    def test_403_on_a_credentialed_request_does_not_pause(
        self, clock, request_with_credential
    ):
        limiter = RateLimiter(rate=100, burst=100)
        http = _http(_answer(403), limiter)

        request_with_credential(http)
        http.get("https://example.test/x")

        assert clock.waited() == 0

    def test_other_failures_do_not_pause(self, clock):
        limiter = RateLimiter(rate=100, burst=100)
        http = _http(_answer(404, 500, 401), limiter)

        for _ in range(4):
            http.get("https://example.test/x")

        assert clock.waited() == 0

    async def test_async_requests_take_tokens_and_honour_the_pause(self, clock):
        limiter = RateLimiter(rate=100, burst=100)
        http = _http(_answer(429), limiter)

        await http.aget("https://example.test/x")
        await http.apost("https://example.test/y", body="")

        assert clock.waited() == pytest.approx(30.0)
        await http.aclose()


class _RecordingLimiter:
    """A ``RequestLimiter`` that is not a ``RateLimiter``: it only takes notes."""

    def __init__(self) -> None:
        self.acquired = 0
        self.recorded: list[tuple[int, float | None]] = []

    def acquire(self) -> None:
        self.acquired += 1

    async def aacquire(self) -> None:
        self.acquired += 1

    def record(self, status: int, retry_after: float | None) -> None:
        self.recorded.append((status, retry_after))


class TestACustomLimiter:
    def test_is_asked_before_each_attempt_and_told_each_response(self, monkeypatch):
        answers = [
            httpx.Response(503, text=""),
            httpx.Response(429, text="", headers={"Retry-After": "7"}),
            httpx.Response(200, text=""),
        ]
        monkeypatch.setattr("app_reviews.core.http.time.sleep", lambda _d: None)
        limiter = _RecordingLimiter()
        http = _http(
            lambda _request: answers.pop(0),
            limiter,
            retry=RetryConfig(max_retries=2, retry_on=(503, 429)),
        )

        response = http.get("https://example.test/x")

        assert response.status == 200
        assert limiter.acquired == 3
        assert limiter.recorded == [(503, None), (429, 7.0), (200, None)]

    def test_a_429_reports_its_retry_after_in_seconds(self):
        limiter = _RecordingLimiter()
        http = _http(_answer(429, headers={"Retry-After": "120"}), limiter)

        response = http.get("https://example.test/x")

        assert response.status == 429
        assert limiter.recorded == [(429, 120.0)]

    def test_a_missing_or_unreadable_retry_after_is_none(self):
        limiter = _RecordingLimiter()
        _http(_answer(429), limiter).get("https://example.test/x")
        _http(_answer(429, headers={"Retry-After": "soon"}), limiter).get(
            "https://example.test/x"
        )

        assert limiter.recorded == [(429, None), (429, None)]

    async def test_async_requests_await_aacquire_and_record(self):
        limiter = _RecordingLimiter()
        http = _http(_answer(429, headers={"Retry-After": "3"}), limiter)

        await http.aget("https://example.test/x")
        await http.apost("https://example.test/y", body="")
        await http.aclose()

        assert limiter.acquired == 2
        assert limiter.recorded == [(429, 3.0), (200, 3.0)]

    def test_no_response_and_a_credentialed_403_are_not_recorded(self):
        def handler(request):
            if request.url.path == "/down":
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(403, text="")

        limiter = _RecordingLimiter()
        http = _http(handler, limiter)

        http.get("https://example.test/down")
        http.get("https://example.test/x", headers={"Authorization": "Bearer t"})
        http.get("https://example.test/x")

        assert limiter.acquired == 3
        assert limiter.recorded == [(403, None)]

    @pytest.mark.parametrize(
        "client_cls",
        [AppStoreReviews, GooglePlayReviews, AppStoreSearch, GooglePlaySearch],
    )
    def test_every_client_hands_it_the_throttled_response(
        self, monkeypatch, client_cls
    ):
        def throttled(_transport, request):
            return httpx.Response(
                429, text="", headers={"Retry-After": "60"}, request=request
            )

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", throttled)
        monkeypatch.setattr("app_reviews.core.http.time.sleep", lambda _d: None)
        limiter = _RecordingLimiter()

        with (
            client_cls(rate_limiter=limiter) as client,
            contextlib.suppress(AppReviewsError),
        ):
            _request(client)

        assert limiter.recorded
        assert limiter.acquired == len(limiter.recorded)
        assert set(limiter.recorded) == {(429, 60.0)}


def _request(client):
    if isinstance(client, AppStoreReviews):
        return client.fetch_page("123", country="us")
    if isinstance(client, GooglePlayReviews):
        return client.fetch_page("com.example.app")
    return client.search("chat")


class TestClientsShareOneLimiter:
    @pytest.mark.parametrize(
        "client_cls",
        [AppStoreReviews, GooglePlayReviews, AppStoreSearch, GooglePlaySearch],
    )
    def test_a_throttled_client_pauses_the_limiter_it_was_given(
        self, clock, monkeypatch, client_cls
    ):
        def blocked(_transport, request):
            return httpx.Response(403, text="", request=request)

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
        limiter = RateLimiter(rate=100, burst=100)

        with (
            client_cls(rate_limiter=limiter) as client,
            contextlib.suppress(AppReviewsError),
        ):
            _request(client)
        limiter.acquire()

        assert clock.waited() == pytest.approx(30.0)

    def test_rate_limiter_alongside_http_is_refused(self):
        with pytest.raises(TypeError, match="rate_limiter"):
            AppStoreReviews(http=HttpClient(), rate_limiter=RateLimiter(rate=1))
