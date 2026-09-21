"""Google Play Developer API provider for reviews."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app_reviews.core.auth import TokenSource
from app_reviews.core.classify import fetch_error_from_response
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient, HttpResponse
from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError
from app_reviews.models.review import Review
from app_reviews.models.types import Source

_LOG = logging.getLogger(__name__)

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
``ValueError``, so one absurd timestamp escaped all the way out of
``fetch_page``, which ``iter_pages`` promises never happens.
"""


class GooglePlayOfficialProvider(PooledClient):
    """Fetches one page from the Google Play Developer API v3.

    Global API: one request, no country dimension, and the API reports no
    storefront per review. Requires a service-account bearer token.

    Ordering is not documented, so ``core.paging.orders_newest_first`` omits
    this source and the ``since`` early stop is never applied to it.
    """

    source: Source = "googleplay_official"

    URL_TEMPLATE = (
        "https://androidpublisher.googleapis.com"
        "/androidpublisher/v3/applications/{app_id}/reviews"
    )

    def __init__(self, auth: TokenSource, *, http: HttpClient | None = None) -> None:
        super().__init__(http=http)
        self._auth = auth

    def fetch_page(self, app_id: str, country: str, cursor: str | None) -> PageResult:
        """Fetch one page. ``cursor`` is the nextPageToken from the last response."""
        url, params = self._request(app_id, cursor)
        response = self._http.get(
            url,
            params=params,
            headers={"Authorization": self._auth.authorization_header()},
        )
        return self._to_page(response, app_id)

    async def afetch_page(
        self, app_id: str, country: str, cursor: str | None
    ) -> PageResult:
        """Async equivalent of ``fetch_page``."""
        url, params = self._request(app_id, cursor)
        response = await self._http.aget(
            url,
            params=params,
            headers={"Authorization": await self._auth.aauthorization_header()},
        )
        return self._to_page(response, app_id)

    def _request(self, app_id: str, cursor: str | None) -> tuple[str, dict[str, str]]:
        """The URL and query params for one page.

        ``app_id`` is escaped because it lands in the *path* of a request that
        carries a bearer token. Left raw, ``..`` segments inside it are
        normalised away by the client and retarget that credentialed request at
        another path on the API. The cursor needs no such care: it is a query
        value, which the client encodes.
        """
        url = self.URL_TEMPLATE.format(app_id=quote(app_id, safe=""))
        params = {"maxResults": "100"}
        if cursor:
            params["token"] = cursor
        return url, params

    def _to_page(self, response: HttpResponse, app_id: str) -> PageResult:
        """Turn one HTTP outcome into a ``PageResult``.

        ``country`` on any error is None: the Developer API reports no storefront
        per review, so there is no country to attribute a failure to.
        """
        if response.transport_error is not None:
            return PageResult(
                error=fetch_error_from_response(
                    country=None,
                    status=response.status,
                    message=response.transport_error,
                    transport_error=response.transport_error,
                )
            )
        if not response.ok:
            return PageResult(
                error=fetch_error_from_response(
                    country=None,
                    status=response.status,
                    message=(
                        f"HTTP {response.status} from the Google Play Developer API"
                    ),
                )
            )

        try:
            data = json.loads(response.body)
            entries = data.get("reviews", [])
            if not isinstance(entries, list):
                raise TypeError(
                    f"'reviews' is {type(entries).__name__}, expected a list"
                )
            next_cursor = self._cursor(data)
        except _UNUSABLE as exc:
            return PageResult(
                error=FetchError(
                    country=None,
                    message=f"Malformed Google Play Developer API response: {exc}",
                    kind="parse",
                    status=response.status,
                )
            )

        # A malformed envelope is a page-level error; a single bad entry is not.
        mapped = (self._review(entry, app_id) for entry in entries)
        reviews = [review for review in mapped if review is not None]
        return PageResult(
            reviews=reviews,
            next_cursor=next_cursor,
            skipped_reviews=len(entries) - len(reviews),
        )

    def _cursor(self, data: dict[str, Any]) -> str | None:
        """The next page token, or None on the last page.

        Absent and null both mean "no more pages"; anything else present is a
        malformed envelope rather than a cursor. The token's type is checked
        because it goes straight back out on the next request.
        """
        pagination = data.get("tokenPagination")
        if pagination is None:
            return None
        if not isinstance(pagination, dict):
            raise TypeError(
                f"'tokenPagination' is {type(pagination).__name__}, expected an object"
            )

        token = pagination.get("nextPageToken")
        if token is None:
            return None
        if not isinstance(token, str):
            raise TypeError(
                f"'nextPageToken' is {type(token).__name__}, expected a string"
            )
        return token

    def _review(self, entry: Any, app_id: str) -> Review | None:
        """Parse one entry, or None if a field that cannot be invented is unusable.

        Returning None rather than raising keeps one malformed entry from costing
        the whole page: a page-level failure discards ``next_cursor`` too, which
        ends the walk and silently truncates the fetch.
        """
        try:
            comment = self._user_comment(entry)
            title, body = self._text(comment.get("text", ""))
            return Review(
                store="googleplay",
                app_id=app_id,
                # The Developer API reports no storefront or reviewer country.
                country=None,
                # No default for starRating: 0 fails Review's 1-5 invariant, so a
                # default could only ever turn a missing field into a raised error.
                rating=int(comment["starRating"]),
                title=title,
                body=body,
                author_name=entry.get("authorName", ""),
                app_version=comment.get("appVersionName"),
                language=comment.get("reviewerLanguage"),
                # The API reports lastModified only; there is no creation date.
                updated_at=self._timestamp(comment["lastModified"]),
                source="googleplay_official",
                raw=entry,
                fetched_at=datetime.now(tz=UTC),
                id=entry["reviewId"],
            )
        except _UNUSABLE as exc:
            _LOG.warning(
                "Skipped review %r for app %s: %s", self._id(entry), app_id, exc
            )
            return None

    def _text(self, value: Any) -> tuple[str | None, str]:
        """Map Play's text, including its documented legacy title separator."""
        if not isinstance(value, str):
            raise TypeError(f"review text is {type(value).__name__}, expected a string")
        if "\t" not in value:
            return None, value
        title, body = value.split("\t", 1)
        return title or None, body

    def _user_comment(self, entry: Any) -> Any:
        """The reviewer's own comment out of the ``comments`` list.

        Once a developer replies, that list holds a ``developerComment`` too.
        Slot 0 is the user's today, but nothing in the API guarantees the order,
        and getting it wrong drops exactly the reviews a team has answered.
        """
        for comment in entry["comments"]:
            if isinstance(comment, dict) and "userComment" in comment:
                return comment["userComment"]
        raise KeyError("userComment")

    def _id(self, entry: Any) -> Any:
        """This entry's review id, so a warning can name it and nothing else.

        Logging the entry put the reviewer's name and the text they wrote into a
        warning line.
        """
        return entry.get("reviewId") if isinstance(entry, dict) else None

    def _timestamp(self, value: Any) -> datetime:
        """Read a protobuf ``Timestamp`` (``{seconds, nanos}``) as one instant.

        ``nanos`` is sub-second precision for that same instant, not a second
        timestamp. ``seconds`` deliberately has no default: defaulting it to 0
        dated a review the API sent no timestamp for to 1970-01-01, and
        ``updated_at`` is the only date this source reports, so ``sort``, ``since``
        and ``until`` all read it, so a wrong one is worse than a dropped review.
        """
        seconds = int(value["seconds"])
        nanos = int(value.get("nanos", 0))
        return datetime.fromtimestamp(seconds, tz=UTC).replace(
            microsecond=nanos // 1000
        )
