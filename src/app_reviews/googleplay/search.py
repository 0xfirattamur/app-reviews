"""Client for searching and looking up Google Play apps."""

from __future__ import annotations

from app_reviews.core.classify import raise_for_http_failure
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpResponse
from app_reviews.core.validation import require_non_negative
from app_reviews.googleplay import store_page
from app_reviews.models.country import Country, normalise_country
from app_reviews.models.metadata import AppMetadata


class GooglePlaySearch(PooledClient):
    """Search and lookup for Google Play apps, by scraping the store pages.

    Satisfies ``SearchClient`` structurally; see that Protocol for the contract.

    Play publishes no search API, so both methods read the JSON that the store
    pages embed for their own JavaScript. This class fetches; ``store_page``
    reads, and reports a page it cannot read rather than answering from it.
    """

    SEARCH_URL = "https://play.google.com/store/search"
    DETAIL_URL = store_page.DETAIL_URL

    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
    """Sent on both requests, so the two present the same agent to Play.

    A hedge rather than a fix for an observed block: Play embedded the same data
    for the package's own default agent when this was measured.
    """

    VERSION = store_page.UNVERSIONED
    """Fallback for an app that publishes no single version, which is what the
    store itself shows for one.

    See ``store_page.UNVERSIONED`` for when Play does publish one.
    """

    SEARCH_DATASET = store_page.SEARCH_DATASET
    DETAIL_DATASET = store_page.DETAIL_DATASET

    def search(
        self,
        query: str,
        *,
        country: Country | str = Country.US,
        limit: int = 50,
    ) -> list[AppMetadata]:
        """Search Play for apps matching ``query`` in ``country``'s market.

        One results page is read; ``limit`` truncates it and never fetches more,
        so asking for more apps than Play lists on that page returns what it lists.
        """
        require_non_negative(limit, "limit")
        if limit == 0:
            return []
        response = self._http.get(
            self.SEARCH_URL,
            params=self._search_params(query, country),
            headers={"User-Agent": self.USER_AGENT},
        )
        return self._parse_search(response)[:limit]

    async def asearch(
        self,
        query: str,
        *,
        country: Country | str = Country.US,
        limit: int = 50,
    ) -> list[AppMetadata]:
        """Async equivalent of ``search``."""
        require_non_negative(limit, "limit")
        if limit == 0:
            return []
        response = await self._http.aget(
            self.SEARCH_URL,
            params=self._search_params(query, country),
            headers={"User-Agent": self.USER_AGENT},
        )
        return self._parse_search(response)[:limit]

    def lookup(
        self,
        app_id: str,
        *,
        country: Country | str = Country.US,
    ) -> AppMetadata | None:
        """Look up an app by package name, or None if Play has no such app."""
        response = self._http.get(
            self.DETAIL_URL,
            params=self._lookup_params(app_id, country),
            headers={"User-Agent": self.USER_AGENT},
        )
        return self._parse_lookup(response, app_id)

    async def alookup(
        self,
        app_id: str,
        *,
        country: Country | str = Country.US,
    ) -> AppMetadata | None:
        """Async equivalent of ``lookup``."""
        response = await self._http.aget(
            self.DETAIL_URL,
            params=self._lookup_params(app_id, country),
            headers={"User-Agent": self.USER_AGENT},
        )
        return self._parse_lookup(response, app_id)

    def _search_params(self, query: str, country: Country | str) -> dict[str, str]:
        return {"q": query, "c": "apps", "hl": "en", "gl": self._market(country)}

    def _lookup_params(self, app_id: str, country: Country | str) -> dict[str, str]:
        return {"id": app_id, "hl": "en", "gl": self._market(country)}

    def _market(self, country: Country | str) -> str:
        """The alpha-2 market to query, defaulting to ``Country.US``.

        ``warn_unknown=False`` because ``Country`` is Apple's storefront
        list: Play serves markets it omits (Serbia, Bosnia, Morocco), and
        those reach ``gl`` as plain strings. Warning would flag a correct call.
        """
        return normalise_country(country, warn_unknown=False) or Country.US.value

    def _parse_search(self, response: HttpResponse) -> list[AppMetadata]:
        raise_for_http_failure(response, "Google Play search", credentialed=False)
        return store_page.parse_search_page(response)

    def _parse_lookup(self, response: HttpResponse, app_id: str) -> AppMetadata | None:
        """The app, or None when Play has no such package.

        Only a 404 means "no such app"; any other page this cannot read is
        reported by ``parse_detail_page``.
        """
        if response.status == 404:
            return None
        raise_for_http_failure(response, "Google Play", credentialed=False)
        return store_page.parse_detail_page(response, app_id)
