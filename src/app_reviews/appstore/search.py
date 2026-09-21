"""Client for searching and looking up App Store apps."""

from __future__ import annotations

import json
import logging
from typing import Any

from app_reviews.core.classify import raise_for_http_failure
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpResponse
from app_reviews.core.search import (
    aget_and_parse,
    get_and_parse,
    scraped_datetime,
    scraped_number,
    scraped_text,
)
from app_reviews.core.validation import require_non_negative
from app_reviews.errors import ParseError
from app_reviews.models.country import Country, normalise_country
from app_reviews.models.metadata import AppMetadata

_LOG = logging.getLogger(__name__)


class AppStoreSearch(PooledClient):
    """Search and lookup for App Store apps via the iTunes APIs.

    Satisfies ``SearchClient`` structurally; see that Protocol for the contract.
    """

    SEARCH_URL = "https://itunes.apple.com/search"
    LOOKUP_URL = "https://itunes.apple.com/lookup"

    def search(
        self,
        query: str,
        *,
        country: Country | str = Country.US,
        limit: int = 50,
    ) -> list[AppMetadata]:
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
        results = self._results(response, "iTunes Search API")
        mapped = (self._metadata(r) for r in results)
        return [app for app in mapped if app is not None]

    def _parse_lookup(self, response: HttpResponse) -> AppMetadata | None:
        """The one result, or None, which is also how "no such app" is reported."""
        results = self._results(response, "iTunes Lookup API")
        return self._metadata(results[0]) if results else None

    def _results(self, response: HttpResponse, api: str) -> list[Any]:
        """The ``results`` array, or ``ParseError`` if the body is unreadable.

        An unreadable body is not an empty result set: these methods raise rather
        than return data, so reporting ``[]`` would be indistinguishable from an
        app that genuinely does not exist.
        """
        raise_for_http_failure(response, api, credentialed=False)
        try:
            results = json.loads(response.body).get("results", [])
        except (AttributeError, json.JSONDecodeError) as exc:
            raise ParseError(
                f"Unreadable response from {api}: {exc}", status=response.status
            ) from exc
        if not isinstance(results, list):
            raise ParseError(
                f"{api} returned 'results' as {type(results).__name__}, "
                f"expected a list",
                status=response.status,
            )
        return results

    def _metadata(self, result: Any) -> AppMetadata | None:
        """Map one iTunes result, or None if it carries no usable id.

        ``app_id`` is the numeric ``trackId`` because the review APIs key off the
        track id, not the bundle id. Without either, there is nothing to look the
        app up by later, so the result is dropped rather than given an invented id.
        """
        if not isinstance(result, dict):
            _LOG.warning(
                "Skipped an iTunes result: expected an object, got %s",
                type(result).__name__,
            )
            return None

        app_id = scraped_text(result.get("trackId")) or scraped_text(
            result.get("bundleId")
        )
        if not app_id:
            # The keys, not the object: the body is remote content and an
            # unbounded amount of it does not belong in a log line.
            _LOG.warning(
                "Skipped an iTunes result: no trackId or bundleId. Keys: %s",
                sorted(result)[:12],
            )
            return None

        return AppMetadata(
            app_id=app_id,
            store="appstore",
            name=scraped_text(result.get("trackName")) or "Unknown",
            developer=scraped_text(result.get("artistName")) or "Unknown",
            category=scraped_text(result.get("primaryGenreName")) or "Unknown",
            price=scraped_text(result.get("formattedPrice")) or "Unknown",
            version=scraped_text(result.get("version")) or "Unknown",
            rating=scraped_number(result.get("averageUserRating"), 0.0),
            rating_count=int(scraped_number(result.get("userRatingCount"), 0)),
            url=scraped_text(result.get("trackViewUrl"))
            or f"https://apps.apple.com/app/id{app_id}",
            icon_url=scraped_text(result.get("artworkUrl512")),
            # Both arrive in the same result dict as ``version``, on every search
            # and lookup, so reading them costs no extra request.
            current_version_release_date=scraped_datetime(
                result.get("currentVersionReleaseDate")
            ),
            first_release_date=scraped_datetime(result.get("releaseDate")),
        )
