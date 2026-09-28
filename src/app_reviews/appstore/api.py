"""The base for clients that call App Store Connect with an ``AppStoreAuth``."""

from __future__ import annotations

import asyncio
import threading

from app_reviews.appstore.auth import ConnectAuth, load_connect_credentials
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient
from app_reviews.core.ratelimit import RequestLimiter
from app_reviews.models.config import AppStoreAuth, RetryConfig


class ConnectAPIClient(PooledClient):
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
        super().__init__(proxy=proxy, retry=retry, http=http, rate_limiter=rate_limiter)
        self._auth = auth
        self._signer: ConnectAuth | None = None
        self._signer_lock = threading.Lock()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._connect_auth().authorization_header()}

    async def _aheaders(self) -> dict[str, str]:
        signer = self._signer
        if signer is None:
            signer = await asyncio.to_thread(self._connect_auth)
        return {"Authorization": await signer.aauthorization_header()}

    def _connect_auth(self) -> ConnectAuth:
        if self._signer is None:
            with self._signer_lock:
                if self._signer is None:
                    self._signer = ConnectAuth(load_connect_credentials(self._auth))
        return self._signer

    def close(self) -> None:
        """Discard the signing key and close only a pool created here."""
        if self._signer is not None:
            self._signer.close()
        super().close()

    async def aclose(self) -> None:
        """Async equivalent of :meth:`close`."""
        if self._signer is not None:
            self._signer.close()
        await super().aclose()
