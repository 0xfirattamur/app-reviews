"""Client for searching and looking up App Store apps."""

from __future__ import annotations

import json
import logging
from typing import Any

from app_reviews.appstore.product_page import parse_version_history
from app_reviews.core.classify import raise_for_http_failure
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpResponse
from app_reviews.core.search import (
    aget_and_parse,
    get_and_parse,
    scraped_datetime,
    scraped_rating,
    scraped_rating_count,
    scraped_text,
)
from app_reviews.core.validation import require_non_negative
from app_reviews.errors import ParseError
from app_reviews.models.country import Country, normalise_country
from app_reviews.models.metadata import AppMetadata, AppVersionEntry

_LOG = logging.getLogger(__name__)

_MAX_LOGGED_KEYS = 12
"""How many of a skipped result's keys a warning names."""


class AppStoreSearch(PooledClient):
    """Search and lookup for App Store apps via the iTunes APIs.

    Satisfies ``SearchClient`` structurally; see that Protocol for the contract.
    ``version_history`` goes beyond it, scraping the public product page.
    """

    SEARCH_URL = "https://itunes.apple.com/search"
    LOOKUP_URL = "https://itunes.apple.com/lookup"
    PRODUCT_PAGE_URL = "https://apps.apple.com/{country}/app/id{app_id}"

    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
    )
    """Sent for the product page, which is built for browsers rather than clients."""

    def search(
        self,
        query: str,
        *,
        country: Country | str = Country.US,
        limit: int = 50,
    ) -> list[AppMetadata]:
        """Search the App Store for apps matching ``query`` in ``country``.

        ``limit`` is sent to iTunes as its own result cap, so at most ``limit``
        apps come back; results without a usable id are skipped.
        """
        require_non_negative(limit, "limit")
        if limit == 0:
            return []
        return get_and_parse(
            self._http,
            self.SEARCH_URL,
            self._search_params(query, country, limit),
            self._parse_search,
        )

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
        return await aget_and_parse(
            self._http,
            self.SEARCH_URL,
            self._search_params(query, country, limit),
            self._parse_search,
        )

    def lookup(
        self,
        app_id: str,
        *,
        country: Country | str = Country.US,
    ) -> AppMetadata | None:
        """Look up an app by numeric trackId or reverse-DNS bundleId."""
        return get_and_parse(
            self._http,
            self.LOOKUP_URL,
            self._lookup_params(app_id, country),
            self._parse_lookup,
        )

    async def alookup(
        self,
        app_id: str,
        *,
        country: Country | str = Country.US,
    ) -> AppMetadata | None:
        """Async equivalent of ``lookup``."""
        return await aget_and_parse(
            self._http,
            self.LOOKUP_URL,
            self._lookup_params(app_id, country),
            self._parse_lookup,
        )

    def version_history(
        self,
        app_id: str,
        *,
        country: Country | str = Country.US,
    ) -> list[AppVersionEntry]:
        """Every version the App Store lists for an app, newest first.

        ``app_id`` is the numeric trackId, the id ``search()`` and ``lookup()``
        return. An app the store does not have (HTTP 404) returns ``[]``, as
        ``lookup()`` returns None, and so does a page with no version history.
        A history this cannot read raises ``ParseError``.

        Scraped from the public product page, since the iTunes APIs report only
        the current version: best-effort, and may break when Apple changes it.
        ``AppStoreVersions`` has the official versions, but without dates.
        """
        return get_and_parse(
            self._http,
            self._product_page_url(app_id, country),
            {},
            parse_version_history,
            headers={"User-Agent": self.USER_AGENT},
        )

    async def aversion_history(
        self,
        app_id: str,
        *,
        country: Country | str = Country.US,
    ) -> list[AppVersionEntry]:
        """Async equivalent of ``version_history``."""
        return await aget_and_parse(
            self._http,
            self._product_page_url(app_id, country),
            {},
            parse_version_history,
            headers={"User-Agent": self.USER_AGENT},
        )

    def _product_page_url(self, app_id: str, country: Country | str) -> str:
        """The product page only exists under the numeric trackId."""
        if not app_id.isdigit():
            raise ValueError(
                f"version_history needs a numeric App Store trackId, got {app_id!r}"
            )
        return self.PRODUCT_PAGE_URL.format(
            country=self._storefront(country), app_id=app_id
        )

    def _search_params(
        self, query: str, country: Country | str, limit: int
    ) -> dict[str, str]:
        return {
            "term": query,
            "entity": "software",
            "country": self._storefront(country),
            "limit": str(limit),
        }

    def _lookup_params(self, app_id: str, country: Country | str) -> dict[str, str]:
        """iTunes Lookup takes ``id`` for numeric ids and ``bundleId`` otherwise.

        Picking the param that matches the input shape lets callers chain
        ``search() -> lookup()`` with whichever id ``search()`` returned.
        """
        id_param = "id" if app_id.isdigit() else "bundleId"
        return {id_param: app_id, "country": self._storefront(country)}

    def _storefront(self, country: Country | str) -> str:
        """The alpha-2 storefront to query, defaulting to ``Country.US``.

        Warns on a code with no iTunes storefront, which for this API is a
        caller error rather than a market Apple does not serve.
        """
        return normalise_country(country) or Country.US.value

    def _parse_search(self, response: HttpResponse) -> list[AppMetadata]:
        """Every usable result. Unusable ones are skipped, not fatal."""
        search_entries = self._read_search_results(response, "iTunes Search API")
        apps: list[AppMetadata] = []
        for search_entry in search_entries:
            app = self._parse_metadata(search_entry)
            if app is not None:
                apps.append(app)
        return apps

    def _parse_lookup(self, response: HttpResponse) -> AppMetadata | None:
        """The one result, or None, which is also how "no such app" is reported."""
        search_entries = self._read_search_results(response, "iTunes Lookup API")
        if not search_entries:
            return None
        return self._parse_metadata(search_entries[0])

    def _read_search_results(self, response: HttpResponse, api: str) -> list[Any]:
        """The ``results`` array, or ``ParseError`` if the body is unreadable.

        An unreadable body is not an empty result set: these methods raise rather
        than return data, so reporting ``[]`` would be indistinguishable from an
        app that genuinely does not exist.
        """
        raise_for_http_failure(response, api, credentialed=False)
        try:
            search_entries = json.loads(response.body).get("results", [])
        except (AttributeError, json.JSONDecodeError) as exc:
            raise ParseError(
                f"Unreadable response from {api}: {exc}", status=response.status
            ) from exc
        if not isinstance(search_entries, list):
            raise ParseError(
                f"{api} returned 'results' as {type(search_entries).__name__}, "
                f"expected a list",
                status=response.status,
            )
        return search_entries

    def _parse_metadata(self, search_entry: Any) -> AppMetadata | None:
        """Map one iTunes result, or None if it carries no usable id.

        ``app_id`` is the numeric ``trackId`` because the review APIs key off the
        track id, not the bundle id. Without either, there is nothing to look the
        app up by later, so the result is dropped rather than given an invented id.
        """
        if not isinstance(search_entry, dict):
            _LOG.warning(
                "Skipped an iTunes result: expected an object, got %s",
                type(search_entry).__name__,
            )
            return None

        track_id = scraped_text(search_entry.get("trackId"))
        app_id = track_id or scraped_text(search_entry.get("bundleId"))
        if not app_id:
            # The keys, not the object: the body is remote content and an
            # unbounded amount of it does not belong in a log line.
            _LOG.warning(
                "Skipped an iTunes result: no trackId or bundleId. Keys: %s",
                sorted(search_entry)[:_MAX_LOGGED_KEYS],
            )
            return None

        return AppMetadata(
            app_id=app_id,
            store="appstore",
            name=scraped_text(search_entry.get("trackName")) or "Unknown",
            developer=scraped_text(search_entry.get("artistName")) or "Unknown",
            category=scraped_text(search_entry.get("primaryGenreName")) or "Unknown",
            price=scraped_text(search_entry.get("formattedPrice")) or "Unknown",
            version=scraped_text(search_entry.get("version")) or "Unknown",
            rating=scraped_rating(search_entry.get("averageUserRating")),
            rating_count=scraped_rating_count(search_entry.get("userRatingCount")),
            url=scraped_text(search_entry.get("trackViewUrl"))
            or f"https://apps.apple.com/app/id{app_id}",
            icon_url=scraped_text(search_entry.get("artworkUrl512")),
            # Both arrive in the same result dict as ``version``, on every search
            # and lookup, so reading them costs no extra request.
            current_version_release_date=scraped_datetime(
                search_entry.get("currentVersionReleaseDate")
            ),
            first_release_date=scraped_datetime(search_entry.get("releaseDate")),
            release_notes=scraped_text(search_entry.get("releaseNotes")),
        )
