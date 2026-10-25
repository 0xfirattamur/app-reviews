"""Google Play Developer API provider for reviews."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app_reviews.core.auth import TokenSource
from app_reviews.core.classify import malformed_page, page_failure_from_response
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient, HttpResponse
from app_reviews.models.page import PageResult
from app_reviews.models.reply import ReviewReply
from app_reviews.models.review import DeviceMetadata, Review
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


def parse_protobuf_timestamp(value: Any) -> datetime:
    """Read a protobuf ``Timestamp`` (``{seconds, nanos}``) as one instant.

    ``nanos`` is sub-second precision for that same instant, not a second
    timestamp. ``seconds`` deliberately has no default: defaulting it to 0
    dated a review the API sent no timestamp for to 1970-01-01, and
    ``updated_at`` is the only date this source reports, so ``sort``, ``since``
    and ``until`` all read it, so a wrong one is worse than a dropped review.
    """
    seconds = int(value["seconds"])
    nanos = int(value.get("nanos", 0))
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanos // 1000)


def parse_developer_reply(review_id: str, text: Any, last_modified: Any) -> ReviewReply:
    """A Play ``developerComment`` (or a reply write's ``result``) as a reply.

    Play applies a reply immediately and gives it no id of its own, so it is
    always ``"published"`` with ``reply_id=None``.
    """
    if not isinstance(text, str):
        raise TypeError(f"reply text is {type(text).__name__}, expected a string")
    return ReviewReply(
        review_id=review_id,
        reply_id=None,
        text=text,
        state="published",
        updated_at=(
            None if last_modified is None else parse_protobuf_timestamp(last_modified)
        ),
    )


_DEVICE_TEXT_FIELDS: dict[str, str] = {
    "productName": "product_name",
    "manufacturer": "manufacturer",
    "deviceClass": "device_class",
    "nativePlatform": "native_platform",
    "cpuModel": "cpu_model",
    "cpuMake": "cpu_make",
}
_DEVICE_COUNT_FIELDS: dict[str, str] = {
    "screenWidthPx": "screen_width_px",
    "screenHeightPx": "screen_height_px",
    "screenDensityDpi": "screen_density_dpi",
    "glEsVersion": "gl_es_version",
    "ramMb": "ram_mb",
}
"""``deviceMetadata`` wire names mapped to ``DeviceMetadata`` fields, by type."""


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

    def __init__(
        self,
        auth: TokenSource,
        *,
        http: HttpClient | None = None,
        translation_language: str | None = None,
    ) -> None:
        super().__init__(http=http)
        self._auth = auth
        self._translation_language = translation_language

    def fetch_page(self, app_id: str, country: str, cursor: str | None) -> PageResult:
        """Fetch one page. ``cursor`` is the nextPageToken from the last response."""
        url, params = self._build_review_request(app_id, cursor)
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
        url, params = self._build_review_request(app_id, cursor)
        response = await self._http.aget(
            url,
            params=params,
            headers={"Authorization": await self._auth.aauthorization_header()},
        )
        return self._to_page(response, app_id)

    def _build_review_request(
        self, app_id: str, cursor: str | None
    ) -> tuple[str, dict[str, str]]:
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
        if self._translation_language is not None:
            params["translationLanguage"] = self._translation_language
        return url, params

    def _to_page(self, response: HttpResponse, app_id: str) -> PageResult:
        """Turn one HTTP outcome into a ``PageResult``.

        ``country`` on any error is None: the Developer API reports no storefront
        per review, so there is no country to attribute a failure to.
        """
        failure = page_failure_from_response(
            response, country=None, source_name="the Google Play Developer API"
        )
        if failure is not None:
            return failure
        try:
            entries, next_cursor = self._parse_envelope(response.body)
        except _UNUSABLE as exc:
            return malformed_page(
                country=None,
                source_name="Google Play Developer API",
                status=response.status,
                exc=exc,
            )
        return self._map_reviews(entries, next_cursor, app_id)

    def _parse_envelope(self, body: str) -> tuple[list[Any], str | None]:
        """The review entries and next page token; raises on a malformed envelope."""
        payload = json.loads(body)
        entries = payload.get("reviews", [])
        if not isinstance(entries, list):
            raise TypeError(f"'reviews' is {type(entries).__name__}, expected a list")
        return entries, self._read_next_cursor(payload)

    def _map_reviews(
        self, entries: list[Any], next_cursor: str | None, app_id: str
    ) -> PageResult:
        """The page's reviews; a single bad entry is skipped, not fatal.

        A malformed envelope fails the whole page, but one bad entry must not:
        failing the page discards ``next_cursor`` and silently ends the walk.
        """
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

    def _read_next_cursor(self, payload: dict[str, Any]) -> str | None:
        """The next page token, or None on the last page.

        Absent and null both mean "no more pages"; anything else present is a
        malformed envelope rather than a cursor. The token's type is checked
        because it goes straight back out on the next request.
        """
        pagination = payload.get("tokenPagination")
        if pagination is None:
            return None
        if not isinstance(pagination, dict):
            raise TypeError(
                f"'tokenPagination' is {type(pagination).__name__}, expected an object"
            )

        next_page_token = pagination.get("nextPageToken")
        if next_page_token is None:
            return None
        if not isinstance(next_page_token, str):
            raise TypeError(
                f"'nextPageToken' is {type(next_page_token).__name__}, "
                "expected a string"
            )
        return next_page_token

    def _parse_review(self, entry: Any, app_id: str) -> Review | None:
        """Parse one entry, or None if a field that cannot be invented is unusable.

        Returning None rather than raising keeps one malformed entry from costing
        the whole page: a page-level failure discards ``next_cursor`` too, which
        ends the walk and silently truncates the fetch.
        """
        try:
            comment = self._user_comment(entry)
            review_id = self._string(entry["reviewId"], "reviewId")
            title, body = self._text(comment.get("text", ""))
            return Review(
                store="googleplay",
                app_id=app_id,
                # The Developer API reports no storefront or reviewer country.
                country=None,
                # No default for starRating: 0 fails Review's 1-5 invariant, so a
                # default could only ever turn a missing field into a raised error.
                rating=self._rating(comment["starRating"]),
                title=title,
                body=body,
                author_name=self._string(entry.get("authorName", ""), "authorName"),
                app_version=self._optional_string(
                    comment.get("appVersionName"), "appVersionName"
                ),
                language=self._optional_string(
                    comment.get("reviewerLanguage"), "reviewerLanguage"
                ),
                # The API reports lastModified only; there is no creation date.
                updated_at=parse_protobuf_timestamp(comment["lastModified"]),
                source="googleplay_official",
                raw=entry,
                fetched_at=datetime.now(tz=UTC),
                id=review_id,
                # Optional enrichments: an unusable one becomes None rather than
                # costing the review, so none of these helpers raise.
                developer_reply=self._read_developer_reply(entry, review_id),
                original_text=_optional_text(comment.get("originalText")),
                device=_optional_text(comment.get("device")),
                device_metadata=_parse_device_metadata(comment.get("deviceMetadata")),
                thumbs_up_count=_optional_count(comment.get("thumbsUpCount")),
                thumbs_down_count=_optional_count(comment.get("thumbsDownCount")),
                app_version_code=_optional_count(comment.get("appVersionCode")),
                android_os_version=_optional_count(comment.get("androidOsVersion")),
            )
        except _UNUSABLE as exc:
            _LOG.warning(
                "Skipped review %r for app %s: %s", self._id(entry), app_id, exc
            )
            return None

    def _rating(self, value: Any) -> int:
        """A wire star rating is an integer; coercion would truncate corruption."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("starRating must be an integer")
        return value

    def _string(self, value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} is {type(value).__name__}, expected a string")
        return value

    def _optional_string(self, value: Any, field: str) -> str | None:
        if value is None:
            return None
        return self._string(value, field)

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

    def _read_developer_reply(
        self, entry: dict[str, Any], review_id: str
    ) -> ReviewReply | None:
        """The ``developerComment`` wherever it sits in ``comments``, or None.

        A reply that cannot be read is logged and dropped; the review it
        answers is still usable.
        """
        reply_comment = _find_developer_comment(entry["comments"])
        if reply_comment is None:
            return None
        developer_comment = reply_comment["developerComment"]
        try:
            return parse_developer_reply(
                review_id,
                developer_comment["text"],
                developer_comment.get("lastModified"),
            )
        except _UNUSABLE as exc:
            _LOG.warning(
                "Ignored an unreadable developer reply on review %r: %s",
                review_id,
                exc,
            )
            return None

    def _id(self, entry: Any) -> Any:
        """This entry's review id, so a warning can name it and nothing else.

        Logging the entry put the reviewer's name and the text they wrote into a
        warning line.
        """
        return entry.get("reviewId") if isinstance(entry, dict) else None


def _find_developer_comment(comments: Any) -> dict[str, Any] | None:
    """The first ``comments`` entry holding a ``developerComment``, or None."""
    for comment in comments:
        if isinstance(comment, dict) and "developerComment" in comment:
            return comment
    return None


def _optional_text(value: Any) -> str | None:
    """A non-empty string, or None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    return value


def _optional_count(value: Any) -> int | None:
    """A non-negative integer, or None. ``True`` is not a count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _parse_device_metadata(device_metadata: Any) -> DeviceMetadata | None:
    """The known ``deviceMetadata`` members, each dropped alone if unusable."""
    if not isinstance(device_metadata, dict):
        return None
    known_fields: dict[str, Any] = {}
    for wire_name, field_name in _DEVICE_TEXT_FIELDS.items():
        known_fields[field_name] = _optional_text(device_metadata.get(wire_name))
    for wire_name, field_name in _DEVICE_COUNT_FIELDS.items():
        known_fields[field_name] = _optional_count(device_metadata.get(wire_name))
    if all(field_value is None for field_value in known_fields.values()):
        return None
    return DeviceMetadata(**known_fields)
