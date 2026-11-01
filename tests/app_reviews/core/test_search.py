"""Tests for the SearchClient protocol.

What used to live here tested that ``abc`` works, that instantiating the ABC
raises TypeError, and that its ``__init__`` stored the three attributes it was
handed. None of that was about this package. A Protocol has nothing to
instantiate, so what matters instead is that the two real clients satisfy it and
nothing more.
"""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app_reviews.appstore.search import AppStoreSearch
from app_reviews.core.http import HttpClient
from app_reviews.core.search import SearchClient
from app_reviews.googleplay.search import GooglePlaySearch

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
    def test_proxy_reaches_the_pool(self, factory):
        """Not observable through a recording transport (it only changes how httpx
        dials), so the pool's attribute is the one thing to read. The retry half is
        covered by behaviour in ``core/test_http.py``."""
        client = factory(proxy="http://proxy:8080")

        assert client._http._proxy == "http://proxy:8080"

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
