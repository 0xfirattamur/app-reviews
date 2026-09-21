"""Google Play web scraper provider for reviews."""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import UTC, datetime
from typing import Any, ClassVar

from app_reviews.core.classify import fetch_error_from_response
from app_reviews.core.http import HttpClient, HttpResponse
from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError
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


class GooglePlayScraperProvider:
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
        self._http = http or HttpClient()

    def fetch_page(self, app_id: str, country: str, cursor: str | None) -> PageResult:
        """Fetch one page. ``cursor`` is the page token from the previous page."""
        url, body = self._request(app_id, country, cursor)
        response = self._http.post(url, body=body, headers=self._HEADERS)
        return self._to_page(response, app_id)

    async def afetch_page(
        self, app_id: str, country: str, cursor: str | None
    ) -> PageResult:
        """Async equivalent of ``fetch_page``."""
        url, body = self._request(app_id, country, cursor)
        response = await self._http.apost(url, body=body, headers=self._HEADERS)
        return self._to_page(response, app_id)

    def _request(
        self, app_id: str, country: str, cursor: str | None
    ) -> tuple[str, str]:
        """The URL and form body for one review-fetch RPC.

        The RPC argument is a serialised protobuf, so its shape is positional and
        opaque; only the sort, the page size and the cursor inside it are ours to
        choose.
        """
        page = [self.PAGE_SIZE, None, cursor] if cursor else [self.PAGE_SIZE]
        argument = json.dumps(
            [None, [2, self.SORT_NEWEST, page, None, [None] * 9], [app_id, 7]]
        )
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
        if response.transport_error is not None:
            return PageResult(
                error=fetch_error_from_response(
                    country=None,
                    status=response.status,
                    message=response.transport_error,
                    transport_error=response.transport_error,
                    credentialed=False,
                )
            )
        if not response.ok:
            return PageResult(
                error=fetch_error_from_response(
                    country=None,
                    status=response.status,
                    message=f"HTTP {response.status} from the Google Play web endpoint",
                    credentialed=False,
                )
            )

        try:
            entries, next_cursor = self._envelope(response.body)
        except (_ParseFailed, *_UNUSABLE) as exc:
            return PageResult(
                error=FetchError(
                    country=None,
                    message=f"Malformed Google Play response: {exc}",
                    kind="parse",
                    status=response.status,
                )
            )

        mapped = (self._review(entry, app_id) for entry in entries)
        reviews = [review for review in mapped if review is not None]
        return PageResult(
            reviews=reviews,
            next_cursor=next_cursor,
            skipped_reviews=len(entries) - len(reviews),
        )

    def _envelope(self, raw: str) -> tuple[list[Any], str | None]:
        """The review entries and next page token in one batchexecute body.

        An unreadable envelope is not an empty page, so this raises rather than
        reporting no reviews: the caller has to be able to tell "this app has no
        more reviews" from "we could not read the response".
        """
        match = self._PREFIX.search(raw)
        if match is None:
            raise _ParseFailed("response is missing the batchexecute prefix")

        try:
            for item in json.loads(match.group(1)):
                if not isinstance(item, list) or len(item) < 3:
                    continue
                if item[0] != "wrb.fr" or item[1] != self.RPC_ID:
                    continue
                if item[2] is None:
                    return [], None
                payload = json.loads(item[2])
                if not isinstance(payload, list):
                    raise _ParseFailed(
                        f"RPC payload is {type(payload).__name__}, expected an array"
                    )
                return self._entries(payload), self._token(payload)
        except _UNUSABLE as exc:
            raise _ParseFailed(f"Google Play response is malformed: {exc}") from exc

        raise _ParseFailed(f"no {self.RPC_ID} envelope in response")

    def _entries(self, payload: list[Any]) -> list[Any]:
        """The review entries, or none of them if this page carries no array."""
        entries = payload[self._REVIEWS] if payload else None
        return entries if isinstance(entries, list) else []

    def _token(self, payload: list[Any]) -> str | None:
        """The next page token, or None on the last page."""
        if len(payload) <= self._PAGINATION:
            return None
        pagination = payload[self._PAGINATION]
        if not isinstance(pagination, list) or len(pagination) <= self._PAGE_TOKEN:
            return None
        token = pagination[self._PAGE_TOKEN]
        return token if isinstance(token, str) else None

    def _review(self, entry: Any, app_id: str) -> Review | None:
        """Parse one entry, or None if a field that cannot be invented is unusable.

        ``country`` is always None: Play reviews are one global corpus, so there
        is no storefront to attribute a review to.
        """
        try:
            return Review(
                store="googleplay",
                app_id=app_id,
                country=None,
                rating=int(entry[self._RATING]),
                title=None,  # Play reviews have no title
                body=entry[self._BODY] or "",
                author_name=entry[self._AUTHOR][self._AUTHOR_NAME],
                app_version=self._app_version(entry),
                # Play's web feed reports creation only. The wire carries nanoseconds;
                # a datetime holds microseconds, so the last three digits are dropped.
                created_at=self._created_at(entry[self._TIMESTAMP]),
                source="googleplay_scraper",
                raw=entry,
                fetched_at=datetime.now(tz=UTC),
                id=entry[self._ID],
            )
        except _UNUSABLE as exc:
            _LOG.warning(
                "Skipped review %r for app %s: %s", self._id(entry), app_id, exc
            )
            return None

    def _id(self, entry: Any) -> Any:
        """This entry's review id, so a warning can name it and nothing else.

        Logging the entry itself put the reviewer's name and the URL of their
        profile photo into a warning line.
        """
        return entry[self._ID] if isinstance(entry, list) and entry else None

    def _created_at(self, timestamp: Any) -> datetime:
        """Read ``[seconds, nanos]`` as one instant, keeping sub-second precision."""
        seconds = timestamp[self._SECONDS]
        nanos = timestamp[self._NANOS] if len(timestamp) > self._NANOS else 0
        return datetime.fromtimestamp(seconds, tz=UTC).replace(
            microsecond=int(nanos or 0) // 1000
        )

    def _app_version(self, entry: list[Any]) -> str | None:
        """The version the reviewer was running, where the entry reports one."""
        if len(entry) <= self._APP_VERSION or not entry[self._APP_VERSION]:
            return None
        return str(entry[self._APP_VERSION])
