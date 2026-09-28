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
        self._token: GoogleAuth | None = None
        self._token_lock = threading.Lock()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._google_auth().authorization_header()}

    async def _aheaders(self) -> dict[str, str]:
        token = self._token
        if token is None:
            token = await asyncio.to_thread(self._google_auth)
        return {"Authorization": await token.aauthorization_header()}

    def _google_auth(self) -> GoogleAuth:
        if self._token is None:
            with self._token_lock:
                if self._token is None:
                    self._token = GoogleAuth(
                        self._auth.service_account_path,
                        service_account_info=self._auth.service_account_info,
                        http=self._http,
                    )
        return self._token

    def close(self) -> None:
        """Discard the cached token and close only a pool created here."""
        if self._token is not None:
            self._token.close()
        super().close()

    async def aclose(self) -> None:
        """Async equivalent of :meth:`close`."""
        if self._token is not None:
            await self._token.aclose()
        await super().aclose()
