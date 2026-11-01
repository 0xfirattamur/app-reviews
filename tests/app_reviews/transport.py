"""The HTTP seam shared by the unit tests.

Every provider and client takes an ``HttpClient``, so a test replaces the network
by giving it an ``httpx.MockTransport``. This module owns the three pieces that
each test file used to rebuild for itself: a recorder for what was sent, a
builder for the client under test, and a reader for a request's query string.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Literal, TypeVar
from urllib.parse import parse_qs

import httpx

from app_reviews import HttpClient, RetryConfig

Handler = Callable[[httpx.Request], httpx.Response]
ClientT = TypeVar("ClientT")


class Recorder:
    """A transport handler that keeps every request it is asked to answer.

    Give it scripted responses, each served once in order with the last one
    repeating, or a ``handler`` that decides per request. ``requests`` holds the
    requests in the order they arrived.
    """

    def __init__(
        self, *responses: httpx.Response, handler: Handler | None = None
    ) -> None:
        if responses and handler is not None:
            raise ValueError("give scripted responses or a handler, not both")
        if not responses and handler is None:
            raise ValueError("give at least one response or a handler")
        self.requests: list[httpx.Request] = []
        self._responses: list[httpx.Response] = list(responses)
        self._handler: Handler | None = handler

    @property
    def last(self) -> httpx.Request:
        """The most recent request; fails the test when none was sent."""
        assert self.requests, "no request was sent"
        return self.requests[-1]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._handler is not None:
            return self._handler(request)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def client_for(
    client_class: Callable[..., ClientT],
    handler: Handler,
    *,
    retries: int = 0,
    **client_arguments: Any,
) -> ClientT:
    """Build ``client_class`` over a mock transport answered by ``handler``.

    Backoff is zero, so a test that exercises retries never sleeps. ``retries``
    defaults to 0 because most tests assert on the first failure; pass the count
    a retry test wants. Constructor arguments the class needs besides ``http``
    (``auth=``, ``translation_language=``) go in ``client_arguments``.
    """
    http = HttpClient(
        transport=httpx.MockTransport(handler),
        retry=RetryConfig(max_retries=retries, backoff_factor=0),
    )
    return client_class(http=http, **client_arguments)


def query_of(request: httpx.Request) -> dict[str, list[str]]:
    """The request's query string as ``{name: [values]}``, decoded."""
    return parse_qs(request.url.query.decode(), keep_blank_values=True)


class Twin:
    """Runs one scenario through a client's sync method or its async twin.

    Every network entry point has both (``fetch_page``/``afetch_page``), and a
    behaviour such as error classification must hold on both. A test takes the
    ``twin`` fixture, which runs it once per path, and calls
    ``await twin.call(client, "fetch_page", ...)``: sync mode calls the method as
    named, async mode awaits the ``a``-prefixed twin.
    """

    def __init__(self, mode: Literal["sync", "async"]) -> None:
        self.mode: Literal["sync", "async"] = mode

    async def call(self, target: Any, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call ``target.method`` in this mode and return what it returns.

        Exceptions propagate unchanged, so ``pytest.raises`` works on both paths.
        """
        if self.mode == "sync":
            return getattr(target, method)(*args, **kwargs)
        async_method = getattr(target, f"a{method}")
        return await async_method(*args, **kwargs)

    def start(
        self, target: Any, method: str, *args: Any, **kwargs: Any
    ) -> asyncio.Task[Any]:
        """Start the call without waiting for it, for tests that observe it midway.

        The sync method runs on a worker thread so it cannot block the test's
        event loop; the async twin runs as a task on that loop.
        """
        if self.mode == "sync":
            work = asyncio.to_thread(getattr(target, method), *args, **kwargs)
            return asyncio.ensure_future(work)
        async_method = getattr(target, f"a{method}")
        return asyncio.ensure_future(async_method(*args, **kwargs))
