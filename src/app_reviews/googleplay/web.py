"""Google Play web scraper provider for reviews."""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import UTC, datetime
from typing import Any, ClassVar

from app_reviews.core.classify import malformed_page, page_failure_from_response
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient, HttpResponse
from app_reviews.models.page import PageResult
from app_reviews.models.review import Review
from app_reviews.models.types import Source

_LOG = logging.getLogger(__name__)


class _ParseFailed(Exception):
    """The batchexecute envelope could not be read at all."""


_UNUSABLE = (
    AttributeError,
    IndexError,
    KeyError,
    OSError,
    OverflowError,
    TypeError,
    ValueError,
)
"""What reading an untyped payload can raise.

``OverflowError`` and ``OSError`` are in here because ``datetime.fromtimestamp``
raises them for a seconds value outside the platform's range, and neither is a
``ValueError``, so a single absurd timestamp escaped all the way out of
``fetch_page``, which ``iter_pages`` promises never happens.
"""


class GooglePlayScraperProvider(PooledClient):
    """Fetches one page of Google Play reviews per call, via the batchexecute RPC.

    Public web endpoint, no credentials. Play reviews are a single global corpus,
    so every call is one request rather than one per country, and they come back
    newest-first.
    """

    source: Source = "googleplay_scraper"

    URL = "https://play.google.com/_/PlayStoreUi/data/batchexecute"
    RPC_ID = "oCPfdb"

    PAGE_SIZE = 200
    """The most reviews one batchexecute call will return."""

    LANG = "en"
    DEFAULT_COUNTRY = "us"
    """``hl`` and ``gl`` select the store's presentation locale, not a review
    filter, so neither changes which reviews come back. ``gl`` is sent only to
    keep the request well-formed when the caller passes the global sentinel.
    """

    SORT_NEWEST = 2
    """Reviews are always requested newest-first, and this is deliberately not a
    knob: ``core.paging.orders_newest_first`` lists this source on the strength
    of it, and a ``since`` walk stops early on that promise. Sorting
    by anything else would truncate those walks without saying so.
    """

    _HEADERS: ClassVar[dict[str, str]] = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    _PREFIX = re.compile(r"\)]}'\n\n([\s\S]+)")

    # Fixed values in the RPC argument, copied from the request Play's own web
    # client sends. Google documents none of them, so they are named for where
    # they sit rather than for what they mean.
    _ARGUMENT_HEAD = 2
    _APP_ID_TAG = 7
    _OPTION_SLOT_COUNT = 9

    # Slots in the decoded RPC payload.
    _REVIEWS = 0
    _PAGINATION = 1
    _PAGE_TOKEN = 1

    # Slots in one review entry.
    _ID = 0
    _AUTHOR = 1
    _AUTHOR_NAME = 0
    _RATING = 2
    _BODY = 4
    _TIMESTAMP = 5
    _SECONDS = 0
    _NANOS = 1
    """``_TIMESTAMP`` holds one protobuf ``Timestamp``, ``[seconds, nanos]``,
    not a ``[created, updated]`` pair. Reading slot 1 as epoch seconds dated
    reviews written yesterday to somewhere in 1970-2001.
    """

    _APP_VERSION = 10

    def __init__(self, *, http: HttpClient | None = None) -> None:
        super().__init__(http=http)

    def fetch_page(self, app_id: str, country: str, cursor: str | None) -> PageResult:
        """Fetch one page. ``cursor`` is the page token from the previous page."""
        url, body = self._build_review_request(app_id, country, cursor)
        response = self._http.post(url, body=body, headers=self._HEADERS)
        return self._to_page(response, app_id)

    async def afetch_page(
        self, app_id: str, country: str, cursor: str | None
    ) -> PageResult:
        """Async equivalent of ``fetch_page``."""
        url, body = self._build_review_request(app_id, country, cursor)
        response = await self._http.apost(url, body=body, headers=self._HEADERS)
        return self._to_page(response, app_id)

    def _build_review_request(
        self, app_id: str, country: str, cursor: str | None
    ) -> tuple[str, str]:
        """The URL and form body for one review-fetch RPC.

        The RPC argument is a serialised protobuf, so its shape is positional and
        opaque; only the sort, the page size and the cursor inside it are ours to
        choose.
        """
        page = [self.PAGE_SIZE, None, cursor] if cursor else [self.PAGE_SIZE]
        options = [None] * self._OPTION_SLOT_COUNT
        sort_and_page = [self._ARGUMENT_HEAD, self.SORT_NEWEST, page, None, options]
        argument = json.dumps([None, sort_and_page, [app_id, self._APP_ID_TAG]])
        url = (
            f"{self.URL}"
            f"?hl={urllib.parse.quote(self.LANG, safe='')}"
            f"&gl={urllib.parse.quote(country or self.DEFAULT_COUNTRY, safe='')}"
        )
        payload = json.dumps([[[self.RPC_ID, argument, None, "generic"]]])
        return url, urllib.parse.urlencode({"f.req": payload})

    def _to_page(self, response: HttpResponse, app_id: str) -> PageResult:
        """Turn one HTTP outcome into a ``PageResult``.

        Errors carry ``country=None``: this source is global, so there is no
        country to attribute a failure to. Matches both official providers.
        """
        failure = page_failure_from_response(
            response,
            country=None,
            source_name="the Google Play web endpoint",
            credentialed=False,
        )
        if failure is not None:
            return failure
        try:
            entries, next_cursor = self._parse_envelope(response.body)
        except (_ParseFailed, *_UNUSABLE) as exc:
            return malformed_page(
                country=None,
                source_name="Google Play",
                status=response.status,
                exc=exc,
            )
        return self._map_reviews(entries, next_cursor, app_id)

    def _parse_envelope(self, body: str) -> tuple[list[Any], str | None]:
        """The review entries and next page token in one batchexecute body.

        An unreadable envelope is not an empty page, so this raises rather than
        reporting no reviews: the caller has to be able to tell "this app has no
        more reviews" from "we could not read the response".
        """
        match = self._PREFIX.search(body)
        if match is None:
            raise _ParseFailed("response is missing the batchexecute prefix")

        try:
            payload = self._find_rpc_payload(json.loads(match.group(1)))
            if payload is None:
                return [], None
            return self._read_review_entries(payload), self._read_next_cursor(payload)
        except _UNUSABLE as exc:
            raise _ParseFailed(f"Google Play response is malformed: {exc}") from exc

    def _find_rpc_payload(self, rows: Any) -> list[Any] | None:
        """This RPC's decoded payload, or None when Play answered it with null.

        A null answer means the app has no more reviews. A body without this
        RPC's row at all is unreadable, and raises.
        """
        for row in rows:
            if not isinstance(row, list) or len(row) < 3:
                continue
            if row[0] != "wrb.fr" or row[1] != self.RPC_ID:
                continue
            return self._decode_review_payload(row[2])
        raise _ParseFailed(f"no {self.RPC_ID} envelope in response")

    def _decode_review_payload(self, encoded_payload: Any) -> list[Any] | None:
        """The RPC row's JSON-in-a-string payload, decoded."""
        if encoded_payload is None:
            return None
        payload = json.loads(encoded_payload)
        if not isinstance(payload, list):
            raise _ParseFailed(
                f"RPC payload is {type(payload).__name__}, expected an array"
            )
        return payload

    def _map_reviews(
        self, entries: list[Any], next_cursor: str | None, app_id: str
    ) -> PageResult:
        """The page's reviews; an unusable entry is skipped, not fatal."""
        reviews: list[Review] = []
        for entry in entries:
            review = self._parse_review(entry, app_id)
            if review is not None:
                reviews.append(review)
        return PageResult(
            reviews=reviews,
            next_cursor=next_cursor,
            skipped_reviews=len(entries) - len(reviews),
        )

    def _read_review_entries(self, payload: list[Any]) -> list[Any]:
        """The review entries; absent/null is empty, wrong present types fail."""
        entries = payload[self._REVIEWS] if payload else None
        if entries is None:
            return []
        if not isinstance(entries, list):
            raise _ParseFailed(
                f"reviews container is {type(entries).__name__}, expected an array"
            )
        return entries

    def _read_next_cursor(self, payload: list[Any]) -> str | None:
        """The next page token, or None on the last page."""
        if len(payload) <= self._PAGINATION:
            return None
        pagination = payload[self._PAGINATION]
        if pagination is None:
            return None
        if not isinstance(pagination, list):
            raise _ParseFailed(
                f"pagination is {type(pagination).__name__}, expected an array"
            )
        if len(pagination) <= self._PAGE_TOKEN:
            raise _ParseFailed("pagination is missing its page-token slot")
        next_page_token = pagination[self._PAGE_TOKEN]
        if next_page_token is None:
            return None
        if not isinstance(next_page_token, str):
            raise _ParseFailed(
                f"page token is {type(next_page_token).__name__}, expected a string"
            )
        return next_page_token

    def _parse_review(self, entry: Any, app_id: str) -> Review | None:
        """Parse one entry, or None if a field that cannot be invented is unusable.

        ``country`` is always None: Play reviews are one global corpus, so there
        is no storefront to attribute a review to.
        """
        try:
            return Review(
                store="googleplay",
                app_id=app_id,
                country=None,
                rating=self._rating(entry[self._RATING]),
                title=None,  # Play reviews have no title
                body=self._body(entry[self._BODY]),
                author_name=self._author_name(entry[self._AUTHOR]),
                app_version=self._app_version(entry),
                # Play's web feed reports creation only. The wire carries nanoseconds;
                # a datetime holds microseconds, so the last three digits are dropped.
                created_at=self._parse_created_at(entry[self._TIMESTAMP]),
                source="googleplay_scraper",
                raw=entry,
                fetched_at=datetime.now(tz=UTC),
                id=self._string(entry[self._ID], "review id"),
            )
        except _UNUSABLE as exc:
            _LOG.warning(
                "Skipped review %r for app %s: %s", self._id(entry), app_id, exc
            )
            return None

    def _rating(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("rating must be an integer")
        return value

    def _string(self, value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} is {type(value).__name__}, expected a string")
        return value

    def _body(self, value: Any) -> str:
        if value is None:
            return ""
        return self._string(value, "review body")

    def _author_name(self, value: Any) -> str:
        if not isinstance(value, list):
            raise TypeError(
                f"author container is {type(value).__name__}, expected an array"
            )
        return self._string(value[self._AUTHOR_NAME], "author name")

    def _id(self, entry: Any) -> Any:
        """This entry's review id, so a warning can name it and nothing else.

        Logging the entry itself put the reviewer's name and the URL of their
        profile photo into a warning line.
        """
        return entry[self._ID] if isinstance(entry, list) and entry else None

    def _parse_created_at(self, timestamp: Any) -> datetime:
        """Read ``[seconds, nanos]`` as one instant, keeping sub-second precision."""
        seconds = timestamp[self._SECONDS]
        nanos = timestamp[self._NANOS] if len(timestamp) > self._NANOS else 0
        return datetime.fromtimestamp(seconds, tz=UTC).replace(
            microsecond=int(nanos or 0) // 1000
        )

    def _app_version(self, entry: list[Any]) -> str | None:
        """The version the reviewer was running, where the entry reports one."""
        if len(entry) <= self._APP_VERSION or entry[self._APP_VERSION] in (None, ""):
            return None
        return self._string(entry[self._APP_VERSION], "app version")
