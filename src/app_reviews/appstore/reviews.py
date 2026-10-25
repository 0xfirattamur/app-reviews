"""Client for fetching App Store reviews."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from typing import get_args

from app_reviews.appstore.auth import ConnectAuth, load_connect_credentials
from app_reviews.appstore.connect import AppStoreOfficialProvider
from app_reviews.appstore.rss import AppStoreScraperProvider
from app_reviews.core.http import HttpClient
from app_reviews.core.provider import ReviewProvider
from app_reviews.core.ratelimit import RequestLimiter
from app_reviews.core.reviews import BaseReviews
from app_reviews.models.config import AppStoreAuth, RetryConfig
from app_reviews.models.country import Country, connect_territory
from app_reviews.models.types import ConnectSort

_NEWEST_FIRST_SORT: ConnectSort = "-createdDate"


class AppStoreReviews(BaseReviews):
    """Client for fetching App Store reviews.

    With ``auth`` it reads the App Store Connect API; without, the public RSS feed.

    With ``auth``, three options narrow or reorder what Connect sends, before
    anything is fetched. They are set per client, so every fetching method and
    page of a walk uses them; resume a cursor with the client that produced it.

    - ``server_ratings``: only these star ratings, such as ``[1, 2]``.
    - ``server_territories``: only these storefronts, as ``Country`` values or
      codes in either alphabet. This is Connect's own filter; ``countries=`` on
      the fetching methods still has no effect on this global source.
    - ``server_sort``: the order Connect pages in (see ``ConnectSort``). Any
      order other than the default ``"-createdDate"`` turns off the early stops
      that rely on newest-first pages, so ``since`` and ``limit`` walks read
      further before they finish.

    The fetching methods' own ``ratings``, ``since``, ``until`` and ``sort`` still
    apply on top, to what Connect returned.
    """

    def __init__(
        self,
        *,
        auth: AppStoreAuth | None = None,
        proxy: str | None = None,
        retry: RetryConfig | None = None,
        http: HttpClient | None = None,
        rate_limiter: RequestLimiter | None = None,
        server_ratings: Collection[int] | None = None,
        server_territories: Collection[Country | str] | None = None,
        server_sort: ConnectSort | None = None,
    ) -> None:
        super().__init__(proxy=proxy, retry=retry, http=http, rate_limiter=rate_limiter)
        has_server_options = (
            server_ratings is not None
            or server_territories is not None
            or server_sort is not None
        )
        if has_server_options and auth is None:
            raise ValueError(
                "server_ratings, server_territories and server_sort need auth: "
                "only the App Store Connect API filters server-side"
            )
        self._auth = auth
        self._server_ratings = _normalise_server_ratings(server_ratings)
        self._server_territories = _normalise_server_territories(server_territories)
        self._server_sort = _normalise_server_sort(server_sort)

    def _build_provider(self) -> ReviewProvider:
        if self._auth is None:
            return AppStoreScraperProvider(**self._provider_kwargs)
        return AppStoreOfficialProvider(
            ConnectAuth(load_connect_credentials(self._auth)),
            server_ratings=self._server_ratings,
            server_territories=self._server_territories,
            server_sort=self._server_sort,
            **self._provider_kwargs,
        )

    async def _abuild_provider(self) -> ReviewProvider:
        """Async construction: keeps a key-file read off the event loop.

        ``_build_provider`` may read the .p8 from disk, which blocks and has no
        async equivalent to await. Signing is not done here; ``ConnectAuth`` signs
        lazily, per request, so it can refresh an expiring token.
        """
        return await asyncio.to_thread(self._build_provider)

    def _is_newest_first(self, provider: ReviewProvider) -> bool:
        """Connect is newest-first only in its default order."""
        if provider.source == "appstore_official":
            return self._server_sort == _NEWEST_FIRST_SORT
        return super()._is_newest_first(provider)


def _normalise_server_ratings(ratings: Collection[int] | None) -> tuple[int, ...]:
    """Sorted, de-duplicated ratings 1-5; empty means every rating."""
    if ratings is None:
        return ()
    if not ratings:
        raise ValueError("server_ratings is empty; omit it to keep every rating")
    for rating in ratings:
        if isinstance(rating, bool) or not isinstance(rating, int):
            raise ValueError(f"server_ratings must be integers, got {rating!r}")
        if not 1 <= rating <= 5:
            raise ValueError(f"server_ratings must be 1-5, got {rating}")
    return tuple(sorted(set(ratings)))


def _normalise_server_territories(
    territories: Collection[Country | str] | None,
) -> tuple[str, ...]:
    """Sorted, de-duplicated alpha-3 territories; empty means every territory."""
    if territories is None:
        return ()
    if not territories:
        raise ValueError("server_territories is empty; omit it to keep every territory")
    alpha3_codes: set[str] = set()
    for territory in territories:
        alpha3 = connect_territory(territory)
        if alpha3 is None:
            raise ValueError(
                f"server_territories has an unknown storefront {territory!r}"
            )
        alpha3_codes.add(alpha3)
    return tuple(sorted(alpha3_codes))


def _normalise_server_sort(sort: ConnectSort | None) -> ConnectSort:
    """The sort, newest first by default; raises on an order Connect lacks."""
    if sort is None:
        return _NEWEST_FIRST_SORT
    connect_sorts = get_args(ConnectSort)
    if sort not in connect_sorts:
        raise ValueError(
            f"server_sort must be one of {', '.join(connect_sorts)}, got {sort!r}"
        )
    return sort
