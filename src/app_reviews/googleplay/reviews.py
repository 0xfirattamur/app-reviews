"""Client for fetching Google Play reviews."""

from __future__ import annotations

import asyncio
from collections.abc import Collection

from app_reviews.core.http import HttpClient
from app_reviews.core.provider import ReviewProvider
from app_reviews.core.reviews import BaseReviews
from app_reviews.googleplay.auth import GoogleAuth
from app_reviews.googleplay.developer_api import GooglePlayOfficialProvider
from app_reviews.googleplay.web import GooglePlayScraperProvider
from app_reviews.models.config import GooglePlayAuth, RetryConfig
from app_reviews.models.country import Country


class GooglePlayReviews(BaseReviews):
    """Client for fetching Google Play reviews.

    With ``auth`` it reads the Developer API; without, the public web endpoint.
    The two are not interchangeable: the web endpoint returns reviews
    newest-first, while the Developer API documents no ordering, so a ``since``
    or ``limit`` walk can stop early on one and never on the other. Both are
    global, so one request covers every storefront.
    """

    def __init__(
        self,
        *,
        auth: GooglePlayAuth | None = None,
        proxy: str | None = None,
        retry: RetryConfig | None = None,
        http: HttpClient | None = None,
    ) -> None:
        super().__init__(proxy=proxy, retry=retry, http=http)
        self._auth = auth

    def _build_provider(self) -> ReviewProvider:
        if self._auth is None:
            return GooglePlayScraperProvider(**self._provider_kwargs)
        return GooglePlayOfficialProvider(
            self._build_google_auth(self._auth), **self._provider_kwargs
        )

    async def _abuild_provider(self) -> ReviewProvider:
        """Async construction: keeps the key-file read off the event loop.

        ``_build_provider`` reads the service-account JSON from disk, which blocks
        and has no async equivalent to await. Signing is not done here;
        ``GoogleAuth`` exchanges lazily, per request, so it can refresh an
        expiring token.
        """
        return await asyncio.to_thread(self._build_provider)

    def _build_google_auth(self, auth: GooglePlayAuth) -> GoogleAuth:
        """Build the auth helper on this client's own connection pool.

        Sharing the pool is what keeps the token exchange on the same proxy and
        retry policy as the review requests, and lets it reuse the connection.
        """
        return GoogleAuth(auth.service_account_path, http=self._http)

    def _validate_country_argument(self, country: Country | str | None) -> None:
        if country is not None:
            raise ValueError(
                "Google Play reviews have no country dimension; omit country"
            )

    def _validate_countries_argument(
        self, countries: Collection[Country | str] | None
    ) -> None:
        if countries is not None:
            raise ValueError(
                "Google Play reviews have no country dimension; omit countries"
            )
