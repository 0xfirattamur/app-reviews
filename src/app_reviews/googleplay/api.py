"""The base for clients that call the Play Developer API with a ``GooglePlayAuth``."""

from __future__ import annotations

import asyncio
import threading

from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient
from app_reviews.core.ratelimit import RequestLimiter
from app_reviews.googleplay.auth import GoogleAuth
from app_reviews.models.config import GooglePlayAuth, RetryConfig


class PlayAPIClient(PooledClient):
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
        super().__init__(proxy=proxy, retry=retry, http=http, rate_limiter=rate_limiter)
        self._auth = auth
        self._cached_google_auth: GoogleAuth | None = None
        self._google_auth_lock = threading.Lock()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._google_auth().authorization_header()}

    async def _aheaders(self) -> dict[str, str]:
        google_auth = self._cached_google_auth
        if google_auth is None:
            google_auth = await asyncio.to_thread(self._google_auth)
        return {"Authorization": await google_auth.aauthorization_header()}

    def _google_auth(self) -> GoogleAuth:
        if self._cached_google_auth is None:
            with self._google_auth_lock:
                if self._cached_google_auth is None:
                    self._cached_google_auth = GoogleAuth(
                        self._auth.service_account_path,
                        service_account_info=self._auth.service_account_info,
                        http=self._http,
                    )
        return self._cached_google_auth

    def close(self) -> None:
        """Discard the cached token and close only a pool created here."""
        if self._cached_google_auth is not None:
            self._cached_google_auth.close()
        super().close()

    async def aclose(self) -> None:
        """Async equivalent of :meth:`close`."""
        if self._cached_google_auth is not None:
            await self._cached_google_auth.aclose()
        await super().aclose()
