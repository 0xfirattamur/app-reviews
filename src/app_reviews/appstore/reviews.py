"""Client for fetching App Store reviews."""

from __future__ import annotations

import asyncio

from app_reviews.appstore.auth import ConnectAuth, load_connect_credentials
from app_reviews.appstore.connect import AppStoreOfficialProvider
from app_reviews.appstore.rss import AppStoreScraperProvider
from app_reviews.core.http import HttpClient
from app_reviews.core.provider import ReviewProvider
from app_reviews.core.ratelimit import RequestLimiter
from app_reviews.core.reviews import BaseReviews
from app_reviews.models.config import AppStoreAuth, RetryConfig


class AppStoreReviews(BaseReviews):
    """Client for fetching App Store reviews.

    With ``auth`` it reads the App Store Connect API; without, the public RSS feed.
    """

    def __init__(
        self,
        *,
        auth: AppStoreAuth | None = None,
        proxy: str | None = None,
        retry: RetryConfig | None = None,
        http: HttpClient | None = None,
        rate_limiter: RequestLimiter | None = None,
    ) -> None:
        super().__init__(proxy=proxy, retry=retry, http=http, rate_limiter=rate_limiter)
        self._auth = auth

    def _build_provider(self) -> ReviewProvider:
        if self._auth is None:
            return AppStoreScraperProvider(**self._provider_kwargs)
        return AppStoreOfficialProvider(
            ConnectAuth(load_connect_credentials(self._auth)), **self._provider_kwargs
        )

    async def _abuild_provider(self) -> ReviewProvider:
        """Async construction: keeps a key-file read off the event loop.

        ``_build_provider`` may read the .p8 from disk, which blocks and has no
        async equivalent to await. Signing is not done here; ``ConnectAuth`` signs
        lazily, per request, so it can refresh an expiring token.
        """
        return await asyncio.to_thread(self._build_provider)
