"""Client for searching and looking up App Store apps."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

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


class AppStoreSearch(PooledClient):
    """Search and lookup for App Store apps via the iTunes APIs.

    Satisfies ``SearchClient`` structurally; see that Protocol for the contract.
    ``version_history`` goes beyond it, reading the public product page.
    """

    SEARCH_URL = "https://itunes.apple.com/search"
    LOOKUP_URL = "https://itunes.apple.com/lookup"
    PRODUCT_PAGE_URL = "https://apps.apple.com/{country}/app/id{app_id}"

    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
    )
    """Sent for the product page, which is built for browsers rather than clients."""

    _SERVER_DATA = re.compile(
        r'<script[^>]*\bid="serialized-server-data"[^>]*>(.*?)</script>', re.DOTALL
    )
    """The JSON the product page embeds to hydrate itself."""

    _VERSION_LABEL = re.compile(r"^Version(?:\s+|$)")
    """The prefix some storefronts render before the number: ``Version 9.1.84``."""

    _RELEASED_AT_FORMAT = "%a %b %d %Y %H:%M:%S GMT%z"
    """A version's date as the page renders it, minus the trailing zone name:
    ``Fri Sep 18 2026 06:25:38 GMT+0000 (Coordinated Universal Time)``."""

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

    def version_history(
        self,
        app_id: str,
        *,
        country: Country | str = Country.US,
    ) -> list[AppVersionEntry]:
        """Every version the App Store lists for an app, newest first.

        ``app_id`` is the numeric trackId, the id ``search()`` and ``lookup()``
        return. Read from the public product page, since the iTunes APIs report
        only the current version. An app the store does not have (HTTP 404)
        returns ``[]``, as ``lookup()`` returns None, and so does a page with no
        version history. A history this cannot read raises ``ParseError``.
        """
        return get_and_parse(
            self._http,
            self._product_page_url(app_id, country),
            {},
            self._parse_version_history,
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
            self._parse_version_history,
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
            rating=scraped_rating(result.get("averageUserRating")),
            rating_count=scraped_rating_count(result.get("userRatingCount")),
            url=scraped_text(result.get("trackViewUrl"))
            or f"https://apps.apple.com/app/id{app_id}",
            icon_url=scraped_text(result.get("artworkUrl512")),
            # Both arrive in the same result dict as ``version``, on every search
            # and lookup, so reading them costs no extra request.
            current_version_release_date=scraped_datetime(
                result.get("currentVersionReleaseDate")
            ),
            first_release_date=scraped_datetime(result.get("releaseDate")),
            release_notes=scraped_text(result.get("releaseNotes")),
        )

    def _parse_version_history(self, response: HttpResponse) -> list[AppVersionEntry]:
        """The page's version history, or ``[]`` for no such app or no history.

        Readable page data without a history block is an app with nothing to
        list. A page whose embedded data is missing or unreadable, or whose
        history holds an entry this cannot read, raises instead: a partial or
        empty answer would pass for a real history.
        """
        if response.status == 404:
            return []
        api = "App Store product page"
        raise_for_http_failure(response, api, credentialed=False)
        match = self._SERVER_DATA.search(response.body)
        if match is None:
            raise ParseError(
                f"{api} carried no embedded page data", status=response.status
            )
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"Unreadable page data from {api}: {exc}", status=response.status
            ) from exc

        action = self._version_history_action(data)
        if action is None:
            return []
        entries = [
            self._version_entry(item, response.status)
            for item in self._version_items(action["pageData"], response.status)
        ]
        entries.sort(key=lambda entry: entry.released_at, reverse=True)
        return entries

    @staticmethod
    def _version_history_action(data: Any) -> dict[str, Any] | None:
        """The action that opens "Version History", which carries its ``pageData``.

        Found by what it is rather than where it sits, since the path to it is
        undocumented page structure.
        """
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("page") == "versionHistory" and "pageData" in node:
                    return node
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return None

    @staticmethod
    def _version_items(page_data: Any, status: int) -> list[Any]:
        shelves = page_data.get("shelves") if isinstance(page_data, dict) else None
        if not isinstance(shelves, list):
            raise ParseError(
                "App Store version history has no list of shelves", status=status
            )
        items: list[Any] = []
        for shelf in shelves:
            shelf_items = shelf.get("items") if isinstance(shelf, dict) else None
            if not isinstance(shelf_items, list):
                raise ParseError(
                    "App Store version history has a shelf without items",
                    status=status,
                )
            items.extend(shelf_items)
        return items

    def _version_entry(self, item: Any, status: int) -> AppVersionEntry:
        """One history entry. Anything missing a version or a date is fatal."""
        if not isinstance(item, dict):
            raise ParseError(
                f"App Store version history entry is {type(item).__name__}, "
                "expected an object",
                status=status,
            )
        label = scraped_text(item.get("primarySubtitle"))
        version = self._VERSION_LABEL.sub("", label) if label else None
        if not version:
            raise ParseError(
                "App Store version history entry has no version", status=status
            )
        released_at = self._released_at(scraped_text(item.get("secondarySubtitle")))
        if released_at is None:
            raise ParseError(
                f"App Store version history entry {version} has no readable date",
                status=status,
            )
        return AppVersionEntry(
            version=version,
            released_at=released_at,
            notes=scraped_text(item.get("text")),
        )

    def _released_at(self, text: str | None) -> datetime | None:
        if text is None:
            return None
        try:
            parsed = datetime.strptime(text.split(" (", 1)[0], self._RELEASED_AT_FORMAT)
        except ValueError:
            return None
        return parsed.astimezone(UTC)
