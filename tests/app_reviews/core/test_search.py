"""Tests for the SearchClient protocol and the shared request-and-parse step.

What used to live here tested that ``abc`` works, that instantiating the ABC
raises TypeError, and that its ``__init__`` stored the three attributes it was
handed. None of that was about this package. A Protocol has nothing to
instantiate, so what matters instead is that the two real clients satisfy it and
that the step they share behaves.
"""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app_reviews.appstore.search import AppStoreSearch
from app_reviews.core.http import HttpClient
from app_reviews.core.search import (
    SearchClient,
    aget_and_parse,
    get_and_parse,
    scraped_number,
)
from app_reviews.googleplay.search import GooglePlaySearch
from app_reviews.models.config import RetryConfig

_METHODS = ("search", "asearch", "lookup", "alookup")


def _conforms(client: SearchClient) -> SearchClient:
    """Typed identity: mypy rejects the call site if ``client`` is not a
    ``SearchClient``, which checks signatures and not merely method names."""
    return client


def _pool(handler):
    return HttpClient(transport=httpx.MockTransport(handler))


class TestProtocolConformance:
    @pytest.mark.parametrize("factory", [AppStoreSearch, GooglePlaySearch])
    def test_the_real_clients_satisfy_the_protocol(self, factory):
        assert _conforms(factory()) is not None

    @pytest.mark.parametrize("factory", [AppStoreSearch, GooglePlaySearch])
    @pytest.mark.parametrize("method", _METHODS)
    def test_every_protocol_method_is_present(self, factory, method):
        assert callable(getattr(factory(), method))


class TestPoolOwnership:
    """Search clients own a pool the same way review clients do."""

    @pytest.mark.parametrize("factory", [AppStoreSearch, GooglePlaySearch])
    def test_proxy_and_retry_configure_the_pool(self, factory):
        retry = RetryConfig(max_retries=5)
        client = factory(proxy="http://proxy:8080", retry=retry)

        assert client._http._proxy == "http://proxy:8080"
        assert client._http._retry is retry

    @pytest.mark.parametrize("factory", [AppStoreSearch, GooglePlaySearch])
    def test_an_injected_pool_is_used_as_given(self, factory):
        pool = HttpClient()

        assert factory(http=pool)._http is pool

    @pytest.mark.parametrize("factory", [AppStoreSearch, GooglePlaySearch])
    def test_close_is_safe_and_repeatable(self, factory):
        client = factory()
        client.close()
        client.close()

    def test_works_as_a_context_manager(self):
        def handler(_request):
            return httpx.Response(200, text='{"results": []}')

        with AppStoreSearch(http=_pool(handler)) as client:
            assert client.search("notes") == []

    async def test_injected_pool_remains_caller_owned(self):
        pool = _pool(lambda _request: httpx.Response(200, text=""))
        pool.close = Mock()
        pool.aclose = AsyncMock()
        client = AppStoreSearch(http=pool)

        client.close()
        await client.aclose()

        pool.close.assert_not_called()
        pool.aclose.assert_not_awaited()


class TestGetAndParse:
    def test_sends_params_and_returns_what_parse_returns(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text="body-text")

        result = get_and_parse(
            _pool(handler),
            "https://example.test/x",
            {"q": "1"},
            lambda response: response.body.upper(),
        )

        assert result == "BODY-TEXT"
        assert "q=1" in seen["url"]

    def test_forwards_headers(self):
        seen = {}

        def handler(request):
            seen["ua"] = request.headers.get("user-agent")
            return httpx.Response(200, text="")

        get_and_parse(
            _pool(handler),
            "https://example.test/x",
            {},
            lambda response: response.body,
            headers={"User-Agent": "custom-agent"},
        )

        assert seen["ua"] == "custom-agent"

    async def test_async_twin_matches(self):
        def handler(_request):
            return httpx.Response(200, text="body-text")

        assert await aget_and_parse(
            _pool(handler),
            "https://example.test/x",
            {},
            lambda response: response.body.upper(),
        ) == get_and_parse(
            _pool(handler),
            "https://example.test/x",
            {},
            lambda response: response.body.upper(),
        )

    def test_a_raising_parse_propagates(self):
        """``_parse_search``/``_parse_lookup`` raise HttpError on a bad
        response; the helper must not swallow that."""

        def handler(_request):
            return httpx.Response(500, text="")

        def parse(_response):
            raise RuntimeError("parse said no")

        with pytest.raises(RuntimeError, match="parse said no"):
            get_and_parse(_pool(handler), "https://example.test/x", {}, parse)


class TestScrapedNumber:
    @pytest.mark.parametrize(
        "value", [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity"]
    )
    def test_non_finite_values_fall_back_to_the_default(self, value):
        assert scraped_number(value, 0.0) == 0.0

    def test_an_integer_too_large_for_a_float_falls_back_to_the_default(self):
        assert scraped_number(10**400, 7.0) == 7.0
