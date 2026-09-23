"""RSS feed provider for App Store reviews."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app_reviews.core.classify import fetch_error_from_response
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient, HttpResponse
from app_reviews.models.country import normalise_country
from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError
from app_reviews.models.review import Review
from app_reviews.models.types import Source

_LOG = logging.getLogger(__name__)


class AppStoreScraperProvider(PooledClient):
    """Fetches one page of App Store RSS reviews per call.

    Public JSON feed, no credentials, one request per country.
    """

    source: Source = "appstore_scraper"
    MAX_PAGES = 10
    """How many pages Apple serves per storefront. Undocumented; measured."""

    URL_TEMPLATE = (
        "https://itunes.apple.com/{country}/rss/customerreviews"
        "/id={app_id}/sortBy=mostRecent/page={page}/json"
    )

    def __init__(self, *, http: HttpClient | None = None) -> None:
        super().__init__(http=http)

    def fetch_page(self, app_id: str, country: str, cursor: str | None) -> PageResult:
        """Fetch one RSS page. ``cursor`` is the page number, None meaning page 1."""
        page = self._resolve_cursor(cursor, country)
        if isinstance(page, PageResult):
            return page

        response = self._http.get(self._url(app_id, country, page))
        return self._to_page(response, app_id, country, page)

    async def afetch_page(
        self, app_id: str, country: str, cursor: str | None
    ) -> PageResult:
        """Async equivalent of ``fetch_page``."""
        page = self._resolve_cursor(cursor, country)
        if isinstance(page, PageResult):
            return page

        response = await self._http.aget(self._url(app_id, country, page))
        return self._to_page(response, app_id, country, page)

    def _url(self, app_id: str, country: str, page: int) -> str:
        """The feed URL for one page.

        Both interpolated values land in the path, so both are escaped: left raw,
        ``..`` segments in either are normalised away by the client and the
        request quietly goes somewhere else, where an empty feed means nothing.
        """
        return self.URL_TEMPLATE.format(
            country=quote(country, safe=""),
            app_id=quote(app_id, safe=""),
            page=page,
        )

    def _resolve_cursor(self, cursor: str | None, country: str) -> int | PageResult:
        """The page to request, or a ``PageResult`` that ends the walk.

        Cursors are persisted verbatim by callers, so an unusable one is reported
        rather than raised: ``iter_pages`` reports page failures instead.
        """
        if cursor is None:
            return 1
        try:
            page = int(cursor)
        except (TypeError, ValueError):
            page = 0
        if page < 1:
            return PageResult(
                error=FetchError(
                    country=country,
                    message=f"Unusable RSS cursor {cursor!r}: expected a page number",
                    kind="parse",
                )
            )
        if page > self.MAX_PAGES:
            return PageResult()
        return page

    def _to_page(
        self, response: HttpResponse, app_id: str, country: str, page: int
    ) -> PageResult:
        """Turn one HTTP outcome into a ``PageResult``."""
        if response.transport_error is not None:
            return PageResult(
                error=fetch_error_from_response(
                    country=country,
                    status=response.status,
                    message=response.transport_error,
                    transport_error=response.transport_error,
                    credentialed=False,
                )
            )
        if not response.ok:
            message = f"HTTP {response.status} from the App Store RSS feed"
            if response.status == 403:
                message += "; access may be blocked or throttled"
            return PageResult(
                error=fetch_error_from_response(
                    country=country,
                    status=response.status,
                    message=message,
                    credentialed=False,
                )
            )

        try:
            entries = self._entries(json.loads(response.body))
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            return PageResult(
                error=FetchError(
                    country=country,
                    message=f"Malformed App Store RSS response: {exc}",
                    kind="parse",
                    status=response.status,
                )
            )

        mapped = (self._review(entry, app_id, country) for entry in entries)
        reviews = [review for review in mapped if review is not None]
        # Gated on what the feed sent, not on what survived mapping: a page whose
        # entries all fail still means Apple has more, and reporting no cursor
        # here would end the walk as "exhausted", meaning no more data.
        next_cursor = str(page + 1) if entries and page < self.MAX_PAGES else None
        return PageResult(
            reviews=reviews,
            next_cursor=next_cursor,
            skipped_reviews=len(entries) - len(reviews),
        )

    def _entries(self, body: Any) -> list[Any]:
        """The feed's entries, always as a list.

        Apple sends ``entry`` as a bare object, not a one-element list, when an app
        has exactly one review, and iterating that dict yields its keys instead.
        """
        entries = body.get("feed", {}).get("entry", [])
        if isinstance(entries, dict):
            return [entries]
        if not isinstance(entries, list):
            raise TypeError(
                f"'entry' is {type(entries).__name__}, expected a list or object"
            )
        return entries

    def _review(self, entry: Any, app_id: str, country: str) -> Review | None:
        """Parse one entry, or None if a field we cannot fake is unusable.

        ``id`` keys deduplication, and ``rating``/``updated`` cannot be invented,
        so an unusable one costs the review. Every other field degrades to ``None``.
        """
        review_id = self._label(entry, "id")
        rating = self._label(entry, "im:rating")
        updated = self._label(entry, "updated")

        try:
            if not review_id:
                raise ValueError("no id, which deduplication keys on")
            if not rating:
                raise ValueError("no rating")
            if not updated:
                raise ValueError("no updated timestamp")

            return Review(
                store="appstore",
                app_id=app_id,
                country=normalise_country(country),
                rating=int(rating),
                title=self._label(entry, "title"),
                body=self._label(entry, "content") or "",
                author_name=self._label(entry, "author", "name") or "",
                app_version=self._label(entry, "im:version"),
                updated_at=datetime.fromisoformat(updated),
                source="appstore_scraper",
                raw=entry,
                fetched_at=datetime.now(tz=UTC),
                id=review_id,
            )
        except ValueError as exc:
            _LOG.warning("Skipped review %r for app %s: %s", review_id, app_id, exc)
            return None

    def _label(self, entry: Any, *path: str) -> str | None:
        """The text at ``path``, unwrapping Atom's ``{"label": ...}`` at each step.

        A missing key, an unexpected shape and an empty string all mean "not
        reported", so all three give ``None`` rather than raising or leaking a
        blank. Line endings are normalised, because Apple embeds CRLF in review bodies.
        """
        node: Any = entry
        for key in (*path, "label"):
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        if not isinstance(node, str):
            return None
        return node.replace("\r\n", "\n").replace("\r", "\n").strip() or None
