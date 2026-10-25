"""App Store Connect API provider for reviews."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

from app_reviews.core.auth import TokenSource
from app_reviews.core.classify import malformed_page, page_failure_from_response
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient, HttpResponse
from app_reviews.models.country import normalise_country
from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError
from app_reviews.models.review import Review
from app_reviews.models.types import Source

_LOG = logging.getLogger(__name__)


class AppStoreOfficialProvider(PooledClient):
    """Fetches one page from the App Store Connect API.

    Global API: one request covers every territory, and each review carries its
    own ``territory``. Requires a signed ES256 JWT.
    """

    source: Source = "appstore_official"

    API_HOST = "api.appstoreconnect.apple.com"
    CURSOR_PATH_PREFIX = "/v1/apps/"
    """A ``links.next`` may only point back at the app-reviews endpoints.

    Host alone is not enough: the request carries the caller's JWT, and a
    tampered cursor could aim it at ``/v1/users`` or any other Connect endpoint
    the key can reach.
    """

    URL_TEMPLATE = (
        "https://api.appstoreconnect.apple.com/v1/apps/{app_id}/customerReviews"
        "?sort=-createdDate&limit=200"
    )

    def __init__(self, auth: TokenSource, *, http: HttpClient | None = None) -> None:
        super().__init__(http=http)
        self._auth = auth

    def fetch_page(self, app_id: str, country: str, cursor: str | None) -> PageResult:
        """Fetch one page. cursor is the ``links.next`` URL from the last page."""
        url = self._resolve_cursor(app_id, cursor)
        if isinstance(url, PageResult):
            return url

        response = self._http.get(
            url, headers={"Authorization": self._auth.authorization_header()}
        )
        return self._to_page(response, app_id)

    async def afetch_page(
        self, app_id: str, country: str, cursor: str | None
    ) -> PageResult:
        """Async equivalent of ``fetch_page``."""
        url = self._resolve_cursor(app_id, cursor)
        if isinstance(url, PageResult):
            return url

        response = await self._http.aget(
            url, headers={"Authorization": await self._auth.aauthorization_header()}
        )
        return self._to_page(response, app_id)

    def _resolve_cursor(self, app_id: str, cursor: str | None) -> str | PageResult:
        """The URL to request, or a ``PageResult`` saying why the cursor is refused.

        Connect hands back a whole URL in ``links.next`` and callers persist it, so
        by the time it returns it is untrusted input. Every request carries a signed
        JWT, so a cursor pointing anywhere else would hand that token to whoever
        owns the host, so the cursor must be https on Connect's own host.
        """
        if cursor is None:
            # Escaped because it lands in the *path* of a request carrying the
            # JWT: left raw, ``..`` segments in it are normalised away by the
            # client and retarget that request at another Connect endpoint.
            return self.URL_TEMPLATE.format(app_id=quote(app_id, safe=""))

        if (refusal := self._cursor_refusal(app_id, cursor)) is not None:
            return PageResult(
                error=FetchError(
                    country=None,
                    message=f"Refusing cursor {cursor!r}: {refusal}",
                    kind="parse",
                )
            )
        return cursor

    def _cursor_refusal(self, app_id: str, cursor: str) -> str | None:
        """Why this cursor cannot be requested, or None if it can.

        ``urlsplit`` raises on a malformed IPv6 literal, so even parsing the
        cursor has to be guarded: the validator exists to *report* a bad cursor,
        and raising out of it defeats the point.

        Control characters are refused before anything else: ``urlsplit`` strips
        them, so a cursor could pass this check and still forge a record in a log
        line that renders the URL.
        """
        if any(ch in cursor for ch in "\r\n\t"):
            return "it contains control characters"
        try:
            parsed = urlsplit(cursor)
        except ValueError as exc:
            return f"it cannot be parsed as a URL ({exc})"
        if parsed.scheme != "https":
            return "expected an https URL"
        if parsed.username is not None or parsed.password is not None:
            return "user-info is not allowed"
        if parsed.hostname != self.API_HOST:
            return f"expected a URL on {self.API_HOST}"
        try:
            port = parsed.port
        except ValueError as exc:
            return f"it has an invalid port ({exc})"
        if port not in (None, 443):
            return f"expected the default port, got {port}"
        expected_path = (
            f"{self.CURSOR_PATH_PREFIX}{quote(app_id, safe='')}/customerReviews"
        )
        if parsed.path != expected_path:
            return f"expected the exact path {expected_path}"
        if parsed.fragment:
            return "fragments are not allowed"
        return None

    def _to_page(self, response: HttpResponse, app_id: str) -> PageResult:
        """Turn one HTTP outcome into a ``PageResult``.

        ``country`` on any error is None: the Connect API is global, so there is no
        country to attribute a failure to.
        """
        failure = page_failure_from_response(
            response, country=None, source_name="the App Store Connect API"
        )
        if failure is not None:
            return failure
        try:
            entries, next_cursor = self._parse_envelope(response.body)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            return malformed_page(
                country=None,
                source_name="App Store Connect",
                status=response.status,
                exc=exc,
            )
        return self._map_reviews(entries, next_cursor, app_id)

    def _parse_envelope(self, body: str) -> tuple[list[Any], str | None]:
        """The review entries and next-page URL; raises on a malformed envelope."""
        payload = json.loads(body)
        entries = payload.get("data", [])
        if not isinstance(entries, list):
            raise TypeError(f"'data' is {type(entries).__name__}, expected a list")
        return entries, self._read_next_cursor(payload)

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

    def _read_next_cursor(self, payload: dict[str, Any]) -> str | None:
        """The ``links.next`` URL, or None on the last page.

        Type-checked because it goes straight back out as the next cursor: an
        unhashable value reached the walk's cycle detection and raised
        ``TypeError`` out of ``iter_pages``.
        """
        links = payload.get("links")
        if links is None:
            return None
        if not isinstance(links, dict):
            raise TypeError(f"'links' is {type(links).__name__}, expected an object")
        next_cursor = links.get("next")
        if next_cursor is None:
            return None
        if not isinstance(next_cursor, str):
            raise TypeError(
                f"'links.next' is {type(next_cursor).__name__}, expected a string"
            )
        return next_cursor

    def _parse_review(self, entry: Any, app_id: str) -> Review | None:
        """Parse one entry, or None if a field we cannot fake is unusable.

        ``id`` keys deduplication, and ``rating``/``createdDate`` cannot be
        invented, so an unusable one costs the review. Everything else degrades.
        """
        try:
            review_id = entry["id"]
            attributes = entry["attributes"]
            if not review_id:
                raise ValueError("no id, which deduplication keys on")
            if not attributes.get("createdDate"):
                raise ValueError("no createdDate")

            return Review(
                store="appstore",
                app_id=app_id,
                country=normalise_country(attributes.get("territory")),
                rating=self._rating(attributes["rating"]),
                title=self._text(attributes.get("title")),
                body=self._text(attributes.get("body")) or "",
                author_name=self._text(attributes.get("reviewerNickname")) or "",
                # Connect reports `createdDate` and nothing about edits.
                created_at=datetime.fromisoformat(attributes["createdDate"]),
                source="appstore_official",
                raw=entry,
                fetched_at=datetime.now(tz=UTC),
                id=review_id,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            # The id, never the entry: an entry carries reviewerNickname, title
            # and body, which do not belong in a log line.
            _LOG.warning(
                "Skipped review %r for app %s: %s", self._id(entry), app_id, exc
            )
            return None

    def _rating(self, value: Any) -> int:
        """Accept only the integer 1..5 domain documented by Connect."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("rating must be an integer")
        if not 1 <= value <= 5:
            raise ValueError(f"rating must be 1-5, got {value}")
        return value

    def _id(self, entry: Any) -> Any:
        """This entry's review id, for a warning that names it and nothing else."""
        return entry.get("id") if isinstance(entry, dict) else None

    def _text(self, value: Any) -> str | None:
        """Trimmed text, or None for anything absent, blank, or not a string.

        Stricter than ``core.search.scraped_text``, which stringifies a number: a
        version of ``1.5`` is worth reporting as ``"1.5"``, but a review title
        that arrives as a number is nonsense and is better dropped than shown.
        """
        return (value.strip() or None) if isinstance(value, str) else None
