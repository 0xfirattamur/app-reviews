"""The base for clients that call App Store Connect with an ``AppStoreAuth``."""

from __future__ import annotations

from app_reviews.appstore.auth import ConnectAuth, load_connect_credentials
from app_reviews.core.auth import LazyTokenSourceClient
from app_reviews.core.http import HttpClient
from app_reviews.core.ratelimit import RequestLimiter
from app_reviews.models.config import AppStoreAuth, RetryConfig


class ConnectAPIClient(LazyTokenSourceClient):
    """Owns a pool and a lazily built ``ConnectAuth`` for one ``AppStoreAuth``.

    The key is loaded at the first request rather than in ``__init__``, so a
    ``key_path`` is read off the event loop on the async path, and a bad key
    surfaces as ``AuthError`` from the call that needed it, as it does for
    ``AppStoreReviews``.
    """

    API_BASE = "https://api.appstoreconnect.apple.com"

    def __init__(
        self,
        auth: AppStoreAuth,
        *,
        http: HttpClient | None = None,
        proxy: str | None = None,
        retry: RetryConfig | None = None,
        rate_limiter: RequestLimiter | None = None,
    ) -> None:
        super().__init__(
            self._build_connect_auth,
            proxy=proxy,
            retry=retry,
            http=http,
            rate_limiter=rate_limiter,
        )
        self._auth = auth

    def _build_connect_auth(self) -> ConnectAuth:
        return ConnectAuth(load_connect_credentials(self._auth))
