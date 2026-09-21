"""Tests for AppStoreReviews and GooglePlayReviews client classes."""

import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app_reviews import AppStoreReviews, GooglePlayReviews
from app_reviews.core.http import HttpClient
from app_reviews.core.provider import ReviewProvider
from app_reviews.models.config import AppStoreAuth, GooglePlayAuth, RetryConfig
from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError
from tests.app_reviews.factories import make_review


def _mock_provider(
    pages: list[PageResult],
    source: str = "appstore_scraper",
):
    """Build a mock provider that returns pages in sequence.

    ``source`` must be a real ``Source`` literal, because the client looks up
    ``core.paging`` for both the walk's stop policy and the
    country fan-out, and an unconfigured ``MagicMock`` attribute is not a valid
    key there.
    """
    provider = MagicMock()
    provider.source = source
    provider.fetch_page.side_effect = pages
    return provider


class TestAppStoreReviews:
    def test_default_construction(self):
        assert AppStoreReviews() is not None

    def test_construction_with_auth(self):
        auth = AppStoreAuth(key_id="K", issuer_id="I", key_path="p.p8")
        assert AppStoreReviews(auth=auth) is not None

    def test_construction_with_retry_config(self):
        assert AppStoreReviews(retry=RetryConfig(max_retries=5)) is not None

    @patch.object(AppStoreReviews, "_build_provider")
    def test_fetch_returns_fetch_result(self, mock_build):
        reviews = [make_review(id="1")]
        mock_build.return_value = _mock_provider(
            [PageResult(reviews=reviews, next_cursor=None)]
        )
        result = AppStoreReviews().fetch("324684580")
        assert len(result) == 1
        assert next(iter(result)).id == "1"

    @patch.object(AppStoreReviews, "_build_provider")
    def test_fetch_default_country_is_us(self, mock_build):
        mock_build.return_value = _mock_provider([PageResult()])
        AppStoreReviews().fetch("324684580")
        provider = mock_build.return_value
        assert [c.args[1] for c in provider.fetch_page.call_args_list] == ["us"]

    @patch.object(AppStoreReviews, "_build_provider")
    def test_fetch_passes_countries(self, mock_build):
        mock_build.return_value = _mock_provider([PageResult(), PageResult()])
        AppStoreReviews().fetch("324684580", countries=["us", "gb"], concurrency=1)
        provider = mock_build.return_value
        assert [c.args[1] for c in provider.fetch_page.call_args_list] == ["us", "gb"]

    @patch.object(AppStoreReviews, "_build_provider")
    def test_fetch_with_limit(self, mock_build):
        reviews = [make_review(id=str(i)) for i in range(10)]
        mock_build.return_value = _mock_provider(
            [PageResult(reviews=reviews, next_cursor=None)]
        )
        result = AppStoreReviews().fetch("324684580", limit=3)
        assert len(result) == 3

    @patch.object(AppStoreReviews, "_build_provider")
    def test_fetch_with_ratings_filter(self, mock_build):
        reviews = [make_review(id="1", rating=5), make_review(id="2", rating=1)]
        mock_build.return_value = _mock_provider(
            [PageResult(reviews=reviews, next_cursor=None)]
        )
        result = AppStoreReviews().fetch("123", ratings=[4, 5])
        assert len(result) == 1
        assert next(iter(result)).rating == 5

    @patch.object(AppStoreReviews, "_build_provider")
    def test_fetch_reusable(self, mock_build):
        """One client serves repeated fetches, on one provider rather than one each.

        The provider is credential-bearing and expensive to construct, so it is
        built once and reused; see ``BaseReviews._ensure_provider``.
        """
        reviews = [make_review(id="1")]
        mock_build.return_value = _mock_provider(
            [PageResult(reviews=reviews), PageResult(reviews=reviews)]
        )
        client = AppStoreReviews()

        first = client.fetch("324684580")
        second = client.fetch("389801252")

        assert len(first) == 1
        assert len(second) == 1
        assert mock_build.call_count == 1

    @patch.object(AppStoreReviews, "_build_provider")
    def test_errors_passed_through(self, mock_build):
        err = FetchError(country="gb", message="timeout", kind="transport")
        mock_build.return_value = _mock_provider([PageResult(error=err)])
        result = AppStoreReviews().fetch("123")
        assert len(result.errors) == 1
        assert result.errors[0].country == "gb"


class TestGooglePlayReviews:
    def test_default_construction(self):
        assert GooglePlayReviews() is not None

    def test_construction_with_auth(self):
        auth = GooglePlayAuth(service_account_path="sa.json")
        assert GooglePlayReviews(auth=auth) is not None

    def test_build_provider_without_auth_uses_the_scraper(self):
        provider = GooglePlayReviews()._build_provider()
        assert provider.source == "googleplay_scraper"

    async def test_abuild_provider_without_auth_uses_the_scraper(self):
        provider = await GooglePlayReviews()._abuild_provider()
        assert provider.source == "googleplay_scraper"

    @patch.object(GooglePlayReviews, "_build_provider")
    def test_fetch_returns_fetch_result(self, mock_build):
        reviews = [make_review(id="1", store="googleplay", source="googleplay_scraper")]
        mock_build.return_value = _mock_provider(
            [PageResult(reviews=reviews, next_cursor=None)],
            source="googleplay_scraper",
        )
        result = GooglePlayReviews().fetch("com.example.app")
        assert len(result) == 1

    @pytest.mark.parametrize(
        "auth",
        [None, GooglePlayAuth(service_account_path="must-not-be-read.json")],
    )
    def test_country_arguments_fail_before_building_either_play_provider(self, auth):
        client = GooglePlayReviews(auth=auth)
        with patch.object(
            client, "_build_provider", side_effect=AssertionError("provider built")
        ) as build:
            with pytest.raises(ValueError, match="no country dimension"):
                client.fetch_page("com.example", country="tr")
            with pytest.raises(ValueError, match="no country dimension"):
                list(client.iter_pages("com.example", country="tr"))
            with pytest.raises(ValueError, match="no country dimension"):
                list(client.iter_reviews("com.example", countries=[]))
            with pytest.raises(ValueError, match="no country dimension"):
                client.fetch("com.example", countries=["tr"])
            with pytest.raises(ValueError, match="no country dimension"):
                client.resolve_countries([])

        build.assert_not_called()

    @pytest.mark.parametrize(
        "auth",
        [None, GooglePlayAuth(service_account_path="must-not-be-read.json")],
    )
    async def test_async_country_arguments_fail_before_provider_or_io(self, auth):
        client = GooglePlayReviews(auth=auth)
        with patch.object(
            client, "_abuild_provider", side_effect=AssertionError("provider built")
        ) as build:
            with pytest.raises(ValueError, match="no country dimension"):
                await client.afetch_page("com.example", country="tr")
            with pytest.raises(ValueError, match="no country dimension"):
                _ = [
                    page
                    async for page in client.aiter_pages("com.example", country="tr")
                ]
            with pytest.raises(ValueError, match="no country dimension"):
                _ = [
                    review
                    async for review in client.aiter_reviews(
                        "com.example", countries=[]
                    )
                ]
            with pytest.raises(ValueError, match="no country dimension"):
                await client.afetch("com.example", countries=["tr"])

        build.assert_not_called()


class _SyncTrapGoogleAuth:
    """Stands in for GoogleAuth: the sync path is a trap that fails the test
    if the async ladder ever calls it; the async path returns a real token.
    """

    def __init__(self, service_account_path, *, http=None) -> None:
        self.service_account_path = service_account_path
        self.http = http

    def authorization_header(self) -> str:
        raise AssertionError("sync path used")

    async def aauthorization_header(self) -> str:
        return "Bearer async-token"


class _CapturingGooglePlayOfficialProvider:
    """Asks its token source per request, and records what it got.

    Holding the source rather than a minted header is the point: the token is
    obtained when the request is made, so the async ladder must reach it through
    ``aauthorization_header``. ``_SyncTrapGoogleAuth`` fails the test if the
    blocking path is taken instead.
    """

    source = "googleplay_official"
    last_header: str | None = None

    def __init__(self, auth, **kwargs) -> None:
        self._auth = auth

    def fetch_page(self, app_id, country, cursor):
        raise AssertionError("sync fetch_page used")

    async def afetch_page(self, app_id, country, cursor):
        _CapturingGooglePlayOfficialProvider.last_header = (
            await self._auth.aauthorization_header()
        )
        return PageResult()


class TestGooglePlayThreadsItsPoolToAuth:
    """FIX: a proxied/retrying GooglePlayReviews must not let the token exchange
    bypass the proxy/retry it was configured with.

    Forwarding ``proxy`` and ``retry`` down two levels was one way to do that;
    handing over the client's own pool is stronger, because there is then only
    one place either setting can live. The end-to-end proof (token and reviews
    on the same transport) is in ``tests/app_reviews/auth/test_google_auth.py``.
    """

    def test_build_provider_hands_google_auth_the_clients_own_pool(self):
        auth = GooglePlayAuth(service_account_path="sa.json")
        client = GooglePlayReviews(
            auth=auth,
            proxy="http://proxy.example.com:8080",
            retry=RetryConfig(max_retries=6),
        )

        with patch("app_reviews.googleplay.reviews.GoogleAuth") as mock_google_auth:
            mock_google_auth.return_value.authorization_header.return_value = "Bearer x"
            client._build_provider()

        mock_google_auth.assert_called_once_with("sa.json", http=client._http)

    def test_the_pool_carries_the_configured_proxy_and_retry(self):
        retry = RetryConfig(max_retries=6)
        client = GooglePlayReviews(
            auth=GooglePlayAuth(service_account_path="sa.json"),
            proxy="http://proxy.example.com:8080",
            retry=retry,
        )

        assert client._http._proxy == "http://proxy.example.com:8080"
        assert client._http._retry is retry


def _rss_feed(entries=1):
    entry = {
        "id": {"label": "1"},
        "im:rating": {"label": "5"},
        "title": {"label": "t"},
        "content": {"label": "b"},
        "author": {"name": {"label": "a"}},
        "im:version": {"label": "1.0"},
        "updated": {"label": "2024-03-15T10:00:00-07:00"},
    }
    return json.dumps({"feed": {"entry": [entry] * entries}})


class TestClientOwnsOnePool:
    """FIX: every request built a fresh ``httpx.Client``, so a ten-page walk
    paid ten TLS handshakes. The client now owns one pool for its lifetime and
    hands it to the provider it builds, which is also the injection seam that
    replaced the ``_transport`` attribute nothing in ``src/`` ever assigned.
    """

    def test_an_injected_pool_carries_requests_through_to_the_provider(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, text=_rss_feed(entries=0))

        client = AppStoreReviews(
            http=HttpClient(transport=httpx.MockTransport(handler))
        )
        client.fetch("324684580", countries=["us"])

        assert len(seen) == 1
        assert "/page=1/" in seen[0]

    def test_a_multi_page_walk_builds_only_one_pool(self):
        def handler(request):
            return httpx.Response(200, text=_rss_feed())

        with patch("app_reviews.core.http.httpx.Client", wraps=httpx.Client) as spy:
            client = AppStoreReviews(
                http=HttpClient(transport=httpx.MockTransport(handler))
            )
            result = client.fetch("324684580", countries=["us"])

        assert result.outcomes[0].pages == 10  # the RSS depth cap
        assert spy.call_count == 1

    def test_proxy_and_retry_configure_the_pool(self):
        retry = RetryConfig(max_retries=7)
        client = AppStoreReviews(proxy="http://proxy.test:8080", retry=retry)

        assert client._http._proxy == "http://proxy.test:8080"
        assert client._http._retry is retry

    def test_close_is_safe_and_repeatable(self):
        client = AppStoreReviews()
        client.close()
        client.close()

    def test_works_as_a_context_manager(self):
        def handler(request):
            return httpx.Response(200, text=_rss_feed(entries=0))

        with AppStoreReviews(
            http=HttpClient(transport=httpx.MockTransport(handler))
        ) as client:
            assert client.fetch("324684580", countries=["us"]).reviews == []

    async def test_works_as_an_async_context_manager(self):
        def handler(request):
            return httpx.Response(200, text=_rss_feed(entries=0))

        async with AppStoreReviews(
            http=HttpClient(transport=httpx.MockTransport(handler))
        ) as client:
            result = await client.afetch("324684580", countries=["us"])

        assert result.reviews == []


class TestCountryFanOutFollowsCapabilities:
    """FIX: "is this source global?" was encoded twice: in
    ``core.paging.is_per_country``.
    ``resolve_countries`` trusted the provider while ``_country_arg`` and
    ``_outcome_country`` trusted the table, so the two could silently disagree.
    The table is now the only home for that fact.
    """

    def test_the_protocol_no_longer_carries_countries(self):
        assert not hasattr(ReviewProvider, "countries")

    def test_a_global_source_collapses_to_the_sentinel(self):
        client = AppStoreReviews()
        provider = _mock_provider([], source="appstore_official")

        with patch.object(client, "_build_provider", return_value=provider):
            assert client.resolve_countries(["us", "gb", "de"]) == [""]

    def test_a_per_country_source_keeps_the_requested_list(self):
        client = AppStoreReviews()
        provider = _mock_provider([], source="appstore_scraper")

        with patch.object(client, "_build_provider", return_value=provider):
            assert client.resolve_countries(["us", "gb"]) == ["us", "gb"]

    def test_a_per_country_source_defaults_to_us(self):
        client = AppStoreReviews()
        provider = _mock_provider([], source="appstore_scraper")

        with patch.object(client, "_build_provider", return_value=provider):
            assert client.resolve_countries() == ["us"]

    def test_fetch_never_asks_the_provider_which_countries_to_walk(self):
        client = AppStoreReviews()
        provider = _mock_provider([PageResult()], source="appstore_official")

        with patch.object(client, "_build_provider", return_value=provider):
            client.fetch("324684580", countries=["us", "gb"])

        provider.countries.assert_not_called()


class TestProviderIsBuiltOnce:
    """FIX: ``_build_provider`` ran on every public call, so reading
    ``client.source`` performed an OAuth token exchange on the Play official
    path and re-signed an ES256 JWT on the App Store one, and the documented
    resume-by-cursor loop re-authenticated on every page.
    """

    def test_source_reuses_the_built_provider(self):
        client = AppStoreReviews()
        with patch.object(
            client, "_build_provider", return_value=_mock_provider([])
        ) as build:
            _ = client.source
            _ = client.source

        assert build.call_count == 1

    def test_every_sync_entry_point_shares_one_provider(self):
        client = AppStoreReviews()
        provider = _mock_provider([PageResult(), PageResult()])
        with patch.object(client, "_build_provider", return_value=provider) as build:
            _ = client.source
            client.resolve_countries()
            client.fetch_page("324684580")
            client.fetch_page("324684580")

        assert build.call_count == 1

    async def test_repeated_async_calls_build_once(self):
        client = AppStoreReviews()
        provider = _mock_provider([])
        provider.afetch_page = AsyncMock(return_value=PageResult())
        with patch.object(client, "_build_provider", return_value=provider) as build:
            await client.afetch_page("324684580")
            await client.afetch_page("324684580")

        assert build.call_count == 1

    async def test_async_reuses_a_synchronously_built_provider(self):
        """A blocking build already paid for must not be paid for again."""
        client = AppStoreReviews()
        provider = _mock_provider([])
        provider.afetch_page = AsyncMock(return_value=PageResult())
        with patch.object(client, "_build_provider", return_value=provider) as build:
            _ = client.source
            await client.afetch_page("324684580")

        assert build.call_count == 1


class TestAppStoreAsyncProviderConstruction:
    """FIX: ``AppStoreReviews._build_provider`` reads the .p8 off disk and signs
    an ES256 JWT. Inheriting ``_abuild_provider`` unchanged ran all of that on
    the event loop for every afetch/afetch_page/aiter_pages call.

    ``GooglePlayReviews`` overrides it for the same reason; see
    ``TestTheReviewClientBuildsOffTheLoop`` in the googleplay auth tests.
    """

    async def test_provider_is_not_built_on_the_event_loop_thread(self):
        auth = AppStoreAuth(key_id="K", issuer_id="I", key_path="k.p8")
        client = AppStoreReviews(auth=auth)
        loop_thread = threading.get_ident()
        built_on: dict[str, int] = {}

        def record_thread():
            built_on["thread"] = threading.get_ident()
            return _mock_provider([PageResult()])

        with patch.object(client, "_build_provider", record_thread):
            await client._abuild_provider()

        assert built_on["thread"] != loop_thread

    async def test_the_built_provider_is_still_returned(self):
        client = AppStoreReviews()
        provider = _mock_provider([PageResult()])

        with patch.object(client, "_build_provider", lambda: provider):
            assert await client._abuild_provider() is provider


class TestGooglePlayAsyncTokenExchange:
    """FIX 1: afetch/afetch_page/aiter_pages must obtain the OAuth token
    through the async path, never the blocking sync one."""

    @patch(
        "app_reviews.googleplay.reviews.GooglePlayOfficialProvider",
        _CapturingGooglePlayOfficialProvider,
    )
    @patch("app_reviews.googleplay.reviews.GoogleAuth", _SyncTrapGoogleAuth)
    async def test_afetch_obtains_its_token_via_the_async_path(self):
        auth = GooglePlayAuth(service_account_path="sa.json")

        result = await GooglePlayReviews(auth=auth).afetch("com.example.app")

        assert result.reviews == []
        assert _CapturingGooglePlayOfficialProvider.last_header == "Bearer async-token"
