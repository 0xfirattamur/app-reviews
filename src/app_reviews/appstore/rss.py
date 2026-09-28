"""RSS feed provider for App Store reviews."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

from app_reviews.appstore.atom import atom_entries
from app_reviews.core.classify import fetch_error_from_response
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient, HttpResponse
from app_reviews.models.country import normalise_country
from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError
from app_reviews.models.review import Review
from app_reviews.models.types import FeedFormat, Source

_LOG = logging.getLogger(__name__)

_THROTTLE_STATUSES = frozenset({403})
"""The feed answers 403 while it throttles an address."""


@dataclass(frozen=True, slots=True)
class _PageRef:
    """One page of one app's reviews in one storefront."""

    app_id: str
    country: str
    page: int

    def next(self) -> _PageRef:
        return _PageRef(self.app_id, self.country, self.page + 1)


class _ShortPageMemory:
    """Remembers the pages that follow a short page, where empty means the end.

    Thread-safe. Bounded, because a walk abandoned midway never collects its mark.
    """

    def __init__(self, capacity: int = 4096) -> None:
        self._capacity = capacity
        self._pages: set[_PageRef] = set()
        self._lock = threading.Lock()

    def remember(self, ref: _PageRef) -> None:
        with self._lock:
            if len(self._pages) >= self._capacity:
                self._pages.clear()
            self._pages.add(ref)

    def take(self, ref: _PageRef) -> bool:
        """Whether ``ref`` follows a short page; forgets it either way."""
        with self._lock:
            if ref in self._pages:
                self._pages.discard(ref)
                return True
            return False


class AppStoreScraperProvider(PooledClient):
    """Fetches one page of App Store RSS reviews per call.

    Public feed, no credentials. The JSON feed sometimes answers an empty or
    unreadable page that the XML feed has in full, so such a page is asked again
    as XML, unless the page before it was short (an empty page there is the real
    end). ``PageResult.feed_format`` says which feed answered.
    """

    source: Source = "appstore_scraper"
    MAX_PAGES = 10
    """How many pages Apple serves per storefront. Undocumented; measured."""

    PAGE_SIZE = 50
    """Entries on a full page. Undocumented; measured."""

    URL_TEMPLATE = (
        "https://itunes.apple.com/{country}/rss/customerreviews"
        "/id={app_id}/sortBy=mostRecent/page={page}/json"
    )

    XML_URL_TEMPLATE = (
        "https://itunes.apple.com/{country}/rss/customerreviews"
        "/id={app_id}/sortBy=mostRecent/page={page}/xml"
    )

    def __init__(self, *, http: HttpClient | None = None) -> None:
        super().__init__(http=http)
        self._short_pages = _ShortPageMemory()

    def fetch_page(self, app_id: str, country: str, cursor: str | None) -> PageResult:
        """Fetch one RSS page. ``cursor`` is the page number, None meaning page 1."""
        ref = self._resolve_cursor(app_id, country, cursor)
        if isinstance(ref, PageResult):
            return ref
        after_short_page = self._short_pages.take(ref)
        response = self._http.get(self._url(ref, "json"))
        result = self._json_page(response, ref)
        if not after_short_page and self._needs_xml(response, result):
            xml = self._http.get(self._url(ref, "xml"))
            result = self._xml_page(xml, ref, fallback=result)
        return self._remembering_short(result, ref)

    async def afetch_page(
        self, app_id: str, country: str, cursor: str | None
    ) -> PageResult:
        """Async equivalent of ``fetch_page``."""
        ref = self._resolve_cursor(app_id, country, cursor)
        if isinstance(ref, PageResult):
            return ref
        after_short_page = self._short_pages.take(ref)
        response = await self._http.aget(self._url(ref, "json"))
        result = self._json_page(response, ref)
        if not after_short_page and self._needs_xml(response, result):
            xml = await self._http.aget(self._url(ref, "xml"))
            result = self._xml_page(xml, ref, fallback=result)
        return self._remembering_short(result, ref)

    def _url(self, ref: _PageRef, feed_format: FeedFormat) -> str:
        """Both ids are escaped: a raw ``..`` would send the request elsewhere."""
        template = self.URL_TEMPLATE if feed_format == "json" else self.XML_URL_TEMPLATE
        return template.format(
            country=quote(ref.country, safe=""),
            app_id=quote(ref.app_id, safe=""),
            page=ref.page,
        )

    def _resolve_cursor(
        self, app_id: str, country: str, cursor: str | None
    ) -> _PageRef | PageResult:
        """The page to request, or a ``PageResult`` that ends the walk.

        An unusable cursor is reported, not raised, as other page failures are.
        """
        if cursor is None:
            return _PageRef(app_id, country, 1)
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
        return _PageRef(app_id, country, page)

    def _json_page(self, response: HttpResponse, ref: _PageRef) -> PageResult:
        failure = self._failure(response, ref.country)
        if failure is not None:
            return failure
        try:
            entries = self._entries(json.loads(response.body))
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            return PageResult(
                error=FetchError(
                    country=ref.country,
                    message=f"Malformed App Store RSS response: {exc}",
                    kind="parse",
                    status=response.status,
                )
            )
        return self._page_of(entries, ref, "json")

    def _xml_page(
        self, response: HttpResponse, ref: _PageRef, *, fallback: PageResult
    ) -> PageResult:
        """The XML feed's page, or ``fallback`` when XML has nothing to add.

        A failed XML request fails the page: passing on the doubtful empty JSON
        page would report the storefront exhausted.
        """
        failure = self._failure(response, ref.country)
        if failure is not None:
            return failure
        try:
            entries = atom_entries(response.body)
        except (ElementTree.ParseError, ValueError) as exc:
            _LOG.warning("Unreadable App Store RSS XML for %s: %s", ref, exc)
            return fallback
        if not entries and fallback.error is None:
            return fallback
        _LOG.info("App Store RSS XML answered %d entries for %s", len(entries), ref)
        return self._page_of(entries, ref, "xml")

    def _failure(self, response: HttpResponse, country: str) -> PageResult | None:
        """The failed page for an exchange that got no usable answer, else None."""
        if response.transport_error is not None:
            message = response.transport_error
        elif not response.ok:
            message = f"HTTP {response.status} from the App Store RSS feed"
            if response.status in _THROTTLE_STATUSES:
                message += "; access may be blocked or throttled"
        else:
            return None
        return PageResult(
            error=fetch_error_from_response(
                country=country,
                status=response.status,
                message=message,
                transport_error=response.transport_error,
                credentialed=False,
                rate_limited_statuses=_THROTTLE_STATUSES,
            )
        )

    def _page_of(
        self, entries: list[Any], ref: _PageRef, feed_format: FeedFormat
    ) -> PageResult:
        mapped = (self._review(entry, ref.app_id, ref.country) for entry in entries)
        reviews = [review for review in mapped if review is not None]
        # Gated on what the feed sent, not on what survived mapping: entries that
        # all fail to map still mean Apple has more.
        more = bool(entries) and ref.page < self.MAX_PAGES
        return PageResult(
            reviews=reviews,
            next_cursor=str(ref.page + 1) if more else None,
            skipped_reviews=len(entries) - len(reviews),
            feed_format=feed_format,
        )

    def _needs_xml(self, response: HttpResponse, result: PageResult) -> bool:
        """The JSON feed answered 2xx, but with no entries or an unreadable body."""
        if not response.ok:
            return False
        return result.error is not None or not (
            result.reviews or result.skipped_reviews
        )

    def _remembering_short(self, result: PageResult, ref: _PageRef) -> PageResult:
        entries = len(result.reviews) + result.skipped_reviews
        if result.next_cursor is not None and entries < self.PAGE_SIZE:
            self._short_pages.remember(ref.next())
        return result

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
