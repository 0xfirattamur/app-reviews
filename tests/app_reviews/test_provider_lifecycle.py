"""Lifecycle contracts for directly constructed provider and auth helpers."""

from unittest.mock import AsyncMock, Mock, mock_open, patch

import pytest

from app_reviews.appstore import (
    AppStoreOfficialProvider,
    AppStoreScraperProvider,
    ConnectAuth,
)
from app_reviews.googleplay import (
    GoogleAuth,
    GooglePlayOfficialProvider,
    GooglePlayScraperProvider,
)
from tests.app_reviews.appstore.test_auth import _make_credentials
from tests.app_reviews.factories import StaticToken
from tests.app_reviews.googleplay.test_auth import _SERVICE_ACCOUNT_JSON


class BorrowedHttp:
    def __init__(self) -> None:
        self.close = Mock()
        self.aclose = AsyncMock()


def _provider_factories():
    return (
        lambda http=None: AppStoreScraperProvider(http=http),
        lambda http=None: AppStoreOfficialProvider(StaticToken(), http=http),
        lambda http=None: GooglePlayScraperProvider(http=http),
        lambda http=None: GooglePlayOfficialProvider(StaticToken(), http=http),
    )


class TestProviderLifecycle:
    @pytest.mark.parametrize("factory", _provider_factories())
    def test_context_manager_closes_an_owned_http_client(self, factory):
        provider = factory()
        provider._http.close = Mock()

        with provider as entered:
            assert entered is provider

        provider._http.close.assert_called_once_with()

    @pytest.mark.parametrize("factory", _provider_factories())
    async def test_async_context_manager_closes_an_owned_http_client(self, factory):
        provider = factory()
        provider._http.aclose = AsyncMock()

        async with provider as entered:
            assert entered is provider

        provider._http.aclose.assert_awaited_once_with()

    @pytest.mark.parametrize("factory", _provider_factories())
    async def test_injected_http_client_remains_caller_owned(self, factory):
        borrowed = BorrowedHttp()
        provider = factory(borrowed)

        provider.close()
        await provider.aclose()

        borrowed.close.assert_not_called()
        borrowed.aclose.assert_not_awaited()


class TestGoogleAuthLifecycle:
    def _auth(self, http=None):
        import json

        data = json.dumps(_SERVICE_ACCOUNT_JSON)
        with patch("builtins.open", mock_open(read_data=data)):
            return GoogleAuth("/fake/key.json", http=http)

    def test_context_manager_closes_owned_http_and_clears_cached_secrets(self):
        auth = self._auth()
        auth._http.close = Mock()
        auth._header = "Bearer cached"

        with auth as entered:
            assert entered is auth

        auth._http.close.assert_called_once_with()
        assert auth._header is None
        assert auth._key is None

    async def test_injected_http_client_remains_caller_owned(self):
        borrowed = BorrowedHttp()
        auth = self._auth(borrowed)

        auth.close()
        await auth.aclose()

        borrowed.close.assert_not_called()
        borrowed.aclose.assert_not_awaited()


class TestConnectAuthLifecycle:
    async def test_sync_and_async_contexts_clear_cached_signing_state(self):
        auth = ConnectAuth(_make_credentials())
        auth.authorization_header()

        with auth as entered:
            assert entered is auth

        assert auth._token is None
        assert auth._key is None

        auth.authorization_header()
        async with auth as entered:
            assert entered is auth

        assert auth._token is None
        assert auth._key is None
