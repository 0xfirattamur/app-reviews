"""Fixtures shared across the unit tests."""

from __future__ import annotations

import itertools
import locale
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.app_reviews.transport import Twin


@pytest.fixture
def german_time_locale() -> Iterator[None]:
    """Switch ``LC_TIME`` to German for one test, then restore it.

    An embedding application may run under any locale, and ``strptime``'s ``%a``
    and ``%b`` follow it. Skips where the machine has no German locale.
    """
    previous = locale.setlocale(locale.LC_TIME)
    try:
        locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")
    except locale.Error:
        pytest.skip("the de_DE.UTF-8 locale is not installed")
    yield
    locale.setlocale(locale.LC_TIME, previous)


@pytest.fixture
def write_file(tmp_path: Path) -> Callable[[str], str]:
    """Write text to a new file under ``tmp_path`` and return its path.

    For tests that hand a path to code reading a key file. Each call makes a
    distinct file; pytest removes the directory with the test's other temp data.
    """
    names = (f"key-{number}" for number in itertools.count())

    def write(contents: str) -> str:
        path = tmp_path / next(names)
        path.write_text(contents)
        return str(path)

    return write


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make ``HttpClient`` retry backoff instant, sync and async.

    Returns the delays it was asked to wait, in order, so a test can assert on
    the backoff schedule without waiting through it. ``HttpClient`` reads
    ``time.sleep`` and ``asyncio.sleep`` through their modules, so these patch
    the stdlib functions for the duration of the test; keep it off tests that
    need a real ``asyncio.sleep``.
    """
    delays: list[float] = []

    async def record_async_delay(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("app_reviews.core.http.time.sleep", delays.append)
    monkeypatch.setattr("app_reviews.core.http.asyncio.sleep", record_async_delay)
    return delays


class FakeClock:
    """One movable clock for everything the package reads time from.

    Sleeping advances it instead of waiting, so a test sees how long the code
    would have waited and never waits itself. ``now`` is the monotonic reading
    (rate limiter); ``time()`` is the wall clock (token expiry) at the same
    moment, starting at a fixed epoch second.
    """

    MONOTONIC_START: float = 1000.0
    WALL_CLOCK_START: float = 1_800_000_000.0

    def __init__(self) -> None:
        self.now: float = self.MONOTONIC_START
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.WALL_CLOCK_START + (self.now - self.MONOTONIC_START)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    async def asleep(self, seconds: float) -> None:
        self.sleep(seconds)

    def advance(self, seconds: float) -> None:
        """Move time forward without anyone having slept."""
        self.now += seconds

    def waited(self) -> float:
        """Total slept since the last call."""
        total = sum(self.slept)
        self.slept.clear()
        return total


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> FakeClock:
    """A ``FakeClock`` installed where the rate limiter and token caches read time.

    Depends on ``no_sleep`` so retry backoff is instant too; the limiter's own
    waits are recorded on the clock (``fake_clock.slept``), not in ``no_sleep``.
    ``time.time`` is patched on the stdlib module, as the auth modules read it.
    """
    clock = FakeClock()
    monkeypatch.setattr("app_reviews.core.ratelimit.monotonic", clock.monotonic)
    monkeypatch.setattr("app_reviews.core.ratelimit.sleep", clock.sleep)
    monkeypatch.setattr("app_reviews.core.ratelimit.asleep", clock.asleep)
    monkeypatch.setattr("app_reviews.appstore.auth.time.time", clock.time)
    return clock


@pytest.fixture(params=["sync", "async"])
def twin(request: pytest.FixtureRequest) -> Twin:
    """Run the test once on the sync path and once on the async twin.

    See ``Twin``: the test is ``async def`` and calls ``await twin.call(...)``.
    """
    return Twin(request.param)


class _KeepAliveHandler(BaseHTTPRequestHandler):
    """Answers every GET with ``ok`` and leaves the connection open."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the test output free of per-request access logs."""


@pytest.fixture
def keep_alive_server() -> Iterator[str]:
    """Base URL of a localhost HTTP/1.1 server that keeps connections open.

    For tests that need a pooled connection to survive between requests, which a
    ``MockTransport`` never opens. The server runs on a daemon thread and is shut
    down after the test.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAliveHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()
