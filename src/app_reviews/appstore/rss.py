"""RSS feed provider for App Store reviews."""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

from app_reviews.core.classify import fetch_error_from_response
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient, HttpResponse
from app_reviews.models.country import normalise_country
from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError
from app_reviews.models.review import Review
from app_reviews.models.types import FeedFormat, Source

_LOG = logging.getLogger(__name__)

_ATOM = "{http://www.w3.org/2005/Atom}"
_ITUNES = "{http://itunes.apple.com/rss}"


class AppStoreScraperProvider(PooledClient):
    """Fetches one page of App Store RSS reviews per call.

    Public feed, no credentials, one request per country and page. The JSON feed
    is asked first. It sometimes answers 200 with no entries, or with a body it
    cannot parse, while the XML (Atom) feed for the same page has them all, so
    such a page is asked again as XML, through the same ``HttpClient`` (retry,
    proxy and rate limiter apply), and the XML entries are used if there are
    any. ``PageResult.feed_format`` says which feed answered.

    That second request is made on page 1, and on a later page unless this
    provider saw the previous page come back short of ``PAGE_SIZE``: after a
    short page, an empty one is the feed's real end. A walk resumed from a
    persisted cursor has no previous page to go by, so it may cost one XML
    request at its end.
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

    _MAX_SHORT_PAGE_MARKS = 4096
    """Bound on remembered short pages, for walks abandoned before their end."""

    def __init__(self, *, http: HttpClient | None = None) -> None:
        super().__init__(http=http)
        self._after_short_page: set[tuple[str, str, int]] = set()
        self._marks_lock = threading.Lock()

    def fetch_page(self, app_id: str, country: str, cursor: str | None) -> PageResult:
        """Fetch one RSS page. ``cursor`` is the page number, None meaning page 1."""
        page = self._resolve_cursor(cursor, country)
        if isinstance(page, PageResult):
            return page

        may_fall_back = self._may_fall_back(app_id, country, page)
        response = self._http.get(self._url(app_id, country, page))
        result = self._to_page(response, app_id, country, page)
        if may_fall_back and self._json_came_back_empty(response, result):
            xml = self._http.get(self._url(app_id, country, page, "xml"))
            result = self._with_xml(result, xml, app_id, country, page)
        return self._noting_short_page(result, app_id, country, page)

    async def afetch_page(
        self, app_id: str, country: str, cursor: str | None
    ) -> PageResult:
        """Async equivalent of ``fetch_page``."""
        page = self._resolve_cursor(cursor, country)
        if isinstance(page, PageResult):
            return page

        may_fall_back = self._may_fall_back(app_id, country, page)
        response = await self._http.aget(self._url(app_id, country, page))
        result = self._to_page(response, app_id, country, page)
        if may_fall_back and self._json_came_back_empty(response, result):
            xml = await self._http.aget(self._url(app_id, country, page, "xml"))
            result = self._with_xml(result, xml, app_id, country, page)
        return self._noting_short_page(result, app_id, country, page)

    def _url(
        self, app_id: str, country: str, page: int, feed_format: FeedFormat = "json"
    ) -> str:
        """The feed URL for one page.

        Both interpolated values land in the path, so both are escaped: left raw,
        ``..`` segments in either are normalised away by the client and the
        request quietly goes somewhere else, where an empty feed means nothing.
        """
        template = self.URL_TEMPLATE if feed_format == "json" else self.XML_URL_TEMPLATE
        return template.format(
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
        """Turn one answer from the JSON feed into a ``PageResult``."""
        failure = self._failure(response, country)
        if failure is not None:
            return failure

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
        return self._page_of(entries, app_id, country, page, "json")

    def _failure(self, response: HttpResponse, country: str) -> PageResult | None:
        """The failed page for an exchange that got no usable answer, else None."""
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
        if response.status == 403:
            return PageResult(
                error=FetchError(
                    country=country,
                    message=(
                        "HTTP 403 from the App Store RSS feed; "
                        "access may be blocked or throttled"
                    ),
                    kind="rate_limited",
                    status=403,
                )
            )
        if not response.ok:
            return PageResult(
                error=fetch_error_from_response(
                    country=country,
                    status=response.status,
                    message=f"HTTP {response.status} from the App Store RSS feed",
                    credentialed=False,
                )
            )
        return None

    def _page_of(
        self,
        entries: list[Any],
        app_id: str,
        country: str,
        page: int,
        feed_format: FeedFormat,
    ) -> PageResult:
        """The ``PageResult`` for one page of entries, in the JSON feed's shape."""
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
            feed_format=feed_format,
        )

    def _json_came_back_empty(self, response: HttpResponse, result: PageResult) -> bool:
        """Whether the JSON feed answered 2xx with no entries or an unreadable body."""
        if not response.ok:
            return False
        return result.error is not None or not (
            result.reviews or result.skipped_reviews
        )

    def _with_xml(
        self,
        json_page: PageResult,
        response: HttpResponse,
        app_id: str,
        country: str,
        page: int,
    ) -> PageResult:
        """The XML feed's page, unless it has nothing to add to the JSON feed's.

        A failed XML request is reported as the page's failure: the JSON feed's
        empty answer is the one in doubt, and passing it on would report a
        storefront that may still have reviews as exhausted. An unreadable XML
        body, or an empty one after an empty JSON page, leaves the JSON feed's
        page standing, so a genuinely empty feed still ends the walk normally.
        """
        failure = self._failure(response, country)
        if failure is not None:
            return failure
        try:
            entries = self._xml_entries(response.body)
        except (ElementTree.ParseError, ValueError) as exc:
            _LOG.warning(
                "Unreadable App Store RSS XML for app %s in %s, page %d: %s",
                app_id,
                country,
                page,
                exc,
            )
            return json_page
        if not entries and json_page.error is None:
            return json_page
        _LOG.info(
            "App Store RSS JSON for app %s in %s, page %d, was empty or unreadable; "
            "the XML feed answered with %d entries",
            app_id,
            country,
            page,
            len(entries),
        )
        return self._page_of(entries, app_id, country, page, "xml")

    def _may_fall_back(self, app_id: str, country: str, page: int) -> bool:
        """False once, for the page after a short one this provider served."""
        key = (app_id, country, page)
        with self._marks_lock:
            if key in self._after_short_page:
                self._after_short_page.discard(key)
                return False
        return True

    def _noting_short_page(
        self, result: PageResult, app_id: str, country: str, page: int
    ) -> PageResult:
        """Remember a short page that still offers a cursor, and return it."""
        entries = len(result.reviews) + result.skipped_reviews
        if result.next_cursor is not None and entries < self.PAGE_SIZE:
            with self._marks_lock:
                if len(self._after_short_page) >= self._MAX_SHORT_PAGE_MARKS:
                    self._after_short_page.clear()
                self._after_short_page.add((app_id, country, page + 1))
        return result

    def _xml_entries(self, body: str) -> list[dict[str, Any]]:
        """The Atom feed's entries, each in the JSON feed's shape.

        One parser then reads both feeds. Each child of ``<entry>`` becomes
        ``{"label": text}``, plus ``"attributes"`` when it has any, under the JSON
        feed's key: the Atom name, or ``im:`` and the name for the iTunes
        namespace. Nested elements such as ``author`` nest the same way. The
        ``type="html"`` rendering of ``content`` is dropped, because the JSON
        feed carries only the text one, and a repeated key keeps its first
        element.

        A document type declaration is refused before parsing: Apple's feed has
        none, and it is how entity-expansion attacks arrive.
        """
        if "<!DOCTYPE" in body or "<!ENTITY" in body:
            raise ValueError("the XML feed declares a document type")
        root = ElementTree.fromstring(body)
        if root.tag != f"{_ATOM}feed":
            raise ValueError(f"the root element is {root.tag!r}, not an Atom feed")
        return [self._xml_node(entry) for entry in root.findall(f"{_ATOM}entry")]

    def _xml_node(self, element: ElementTree.Element) -> dict[str, Any]:
        node: dict[str, Any] = {}
        for child in element:
            key = self._xml_key(child.tag)
            if key is None or key in node:
                continue
            if key == "content" and child.get("type") == "html":
                continue
            node[key] = self._xml_value(child)
        return node

    def _xml_value(self, element: ElementTree.Element) -> dict[str, Any]:
        if len(element):
            return self._xml_node(element)
        value: dict[str, Any] = {}
        if element.text is not None:
            value["label"] = element.text
        if element.attrib:
            value["attributes"] = dict(element.attrib)
        return value

    def _xml_key(self, tag: str) -> str | None:
        """The JSON feed's key for an element, or None outside both namespaces."""
        if tag.startswith(_ATOM):
            return tag.removeprefix(_ATOM)
        if tag.startswith(_ITUNES):
            return "im:" + tag.removeprefix(_ITUNES)
        return None

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
