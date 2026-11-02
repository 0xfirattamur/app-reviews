"""The base for clients that call the Play Developer API with a ``GooglePlayAuth``."""

from __future__ import annotations

from app_reviews.core.auth import LazyTokenSourceClient
from app_reviews.core.http import HttpClient
from app_reviews.core.ratelimit import RequestLimiter
from app_reviews.googleplay.auth import GoogleAuth
from app_reviews.models.config import GooglePlayAuth, RetryConfig


class PlayAPIClient(LazyTokenSourceClient):
    """Owns a pool and a lazily built ``GoogleAuth`` for one ``GooglePlayAuth``.

    The key is loaded at the first request, off the event loop on the async
    path. The token exchange shares this client's pool, so proxy, retry and rate
    limiter apply to it too.
    """

    API_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3"

    def __init__(
        self,
        auth: GooglePlayAuth,
        *,
        http: HttpClient | None = None,
        proxy: str | None = None,
        retry: RetryConfig | None = None,
        rate_limiter: RequestLimiter | None = None,
    ) -> None:
        super().__init__(
            self._build_google_auth,
            proxy=proxy,
            retry=retry,
            http=http,
            rate_limiter=rate_limiter,
        )
        self._auth = auth

    def _build_google_auth(self) -> GoogleAuth:
        return GoogleAuth(
            self._auth.service_account_path,
            service_account_info=self._auth.service_account_info,
            http=self._http,
        )
