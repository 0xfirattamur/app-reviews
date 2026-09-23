"""Tests for GooglePlayOfficialProvider."""

import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from app_reviews.core.http import HttpClient
from app_reviews.googleplay.developer_api import GooglePlayOfficialProvider
from tests.app_reviews.factories import StaticToken


def _dev_entry(
    review_id: str = "gp-review-1",
    author: str = "Dave",
    rating: Any = 3,
    body: str = "It's fine",
    seconds: Any = 1710500000,
    app_version: str = "5.0.1",
    reviewer_language: str | None = None,
) -> dict[str, Any]:
    entry = {
        "reviewId": review_id,
        "authorName": author,
        "comments": [
            {
                "userComment": {
                    "text": body,
                    "starRating": rating,
                    "appVersionName": app_version,
                    "lastModified": {"seconds": seconds},
                }
            }
        ],
    }
    if reviewer_language is not None:
        entry["comments"][0]["userComment"]["reviewerLanguage"] = reviewer_language
    return entry


def _payload(entries: list[dict[str, Any]], token: str | None = None) -> str:
    body: dict[str, Any] = {"reviews": entries}
    if token:
        body["tokenPagination"] = {"nextPageToken": token}
    return json.dumps(body)


def _provider(handler):
    """Build a provider whose pooled HTTP client uses a MockTransport."""
    return GooglePlayOfficialProvider(
        StaticToken("Bearer gtoken"),
        http=HttpClient(transport=httpx.MockTransport(handler)),
    )


def _serving(text: str, status: int = 200):
    return _provider(lambda request: httpx.Response(status, text=text))


def _url_for(app_id: str) -> str:
    """The URL the provider actually requests for ``app_id``."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, text=_payload([]))

    _provider(handler).fetch_page(app_id, "", None)
    return seen["url"]


class TestSource:
    def test_source(self):
        assert GooglePlayOfficialProvider(StaticToken()).source == "googleplay_official"


class TestFetchPage:
    def test_sends_authorization_header(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, text=_payload([_dev_entry()]))

        _provider(handler).fetch_page("com.example.app", "", None)

        assert seen["auth"] == "Bearer gtoken"

    def test_requests_the_reviews_endpoint_for_the_app(self):
        assert "/applications/com.example.app/reviews" in _url_for("com.example.app")

    def test_cursor_is_sent_as_the_token_param(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_payload([]))

        _provider(handler).fetch_page("com.example.app", "", "TOK9")

        assert "token=TOK9" in seen["url"]

    def test_no_token_param_on_the_first_page(self):
        assert "token=" not in _url_for("com.example.app")

    def test_requests_the_maximum_supported_page_size(self):
        assert "maxResults=100" in _url_for("com.example.app")

    def test_maps_entry_fields(self):
        page = _serving(_payload([_dev_entry()])).fetch_page(
            "com.example.app", "", None
        )
        review = page.reviews[0]

        assert review.id == "gp-review-1"
        assert review.rating == 3
        assert review.body == "It's fine"
        assert review.author_name == "Dave"
        assert review.app_version == "5.0.1"
        assert review.source == "googleplay_official"
        assert review.raw == _dev_entry()

    def test_country_is_none_because_the_api_does_not_report_it(self):
        page = _serving(_payload([_dev_entry()])).fetch_page(
            "com.example.app", "", None
        )

        assert page.reviews[0].country is None

    def test_title_is_none_because_google_play_has_no_titles(self):
        page = _serving(_payload([_dev_entry()])).fetch_page(
            "com.example.app", "", None
        )

        assert page.reviews[0].title is None

    def test_maps_reviewer_language_without_treating_it_as_country(self):
        page = _serving(_payload([_dev_entry(reviewer_language="tr")])).fetch_page(
            "com.example.app", "", None
        )

        assert page.reviews[0].language == "tr"
        assert page.reviews[0].country is None

    def test_legacy_tab_separates_title_from_body(self):
        page = _serving(
            _payload([_dev_entry(body="A legacy title\tThe review body")])
        ).fetch_page("com.example.app", "", None)

        assert page.reviews[0].title == "A legacy title"
        assert page.reviews[0].body == "The review body"

    def test_nanos_become_sub_second_precision(self):
        entry = _dev_entry()
        entry["comments"][0]["userComment"]["lastModified"]["nanos"] = 839_000_000
        page = _serving(_payload([entry])).fetch_page("com.example.app", "", None)

        assert page.reviews[0].updated_at.microsecond == 839_000

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("reviewId", 123),
            ("authorName", ["Dave"]),
            ("text", {"body": "fine"}),
            ("appVersionName", 501),
            ("reviewerLanguage", ["tr"]),
            ("starRating", 4.5),
            ("starRating", True),
        ],
    )
    def test_wrong_typed_present_scalar_skips_the_review(self, field, value):
        entry = _dev_entry(reviewer_language="en")
        if field in {"reviewId", "authorName"}:
            entry[field] = value
        else:
            entry["comments"][0]["userComment"][field] = value

        page = _serving(_payload([entry])).fetch_page("com.example.app", "", None)

        assert page.reviews == []
        assert page.skipped_reviews == 1


class TestAppIdIsNotAPathInjection:
    """``app_id`` lands in the URL path of a request carrying a bearer token.

    Unescaped, ``..`` segments in it let a caller retarget that request at other
    paths on the API, because httpx normalises the traversal away before sending.
    """

    def test_a_normal_package_name_is_untouched(self):
        assert "/applications/com.example.app/reviews" in _url_for("com.example.app")

    @pytest.mark.parametrize(
        "app_id",
        [
            "../../../../v3/applications/victim.app/reviews",
            "a/../../../../../../evil",
            "com.example.app/../../other",
        ],
    )
    def test_traversal_cannot_leave_the_reviews_endpoint(self, app_id):
        url = _url_for(app_id)

        assert url.startswith(
            "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        )
        assert urlsplit(url).path.endswith("/reviews")
        assert "/v3/applications/victim.app" not in url
        assert "/evil" not in url


class TestPagination:
    def test_next_cursor_comes_from_token_pagination(self):
        body = _payload([_dev_entry()], token="NXT")
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert page.next_cursor == "NXT"

    def test_missing_pagination_ends_the_walk(self):
        page = _serving(_payload([_dev_entry()])).fetch_page(
            "com.example.app", "", None
        )

        assert page.next_cursor is None

    def test_null_pagination_is_the_last_page_not_a_failure(self):
        """``.get(key, default)`` only falls back when the key is *absent*, so a
        present-but-null ``tokenPagination`` turned the last page into an error."""
        body = json.dumps({"reviews": [_dev_entry()], "tokenPagination": None})
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert page.error is None
        assert page.next_cursor is None
        assert len(page.reviews) == 1

    def test_null_next_page_token_is_the_last_page(self):
        body = json.dumps(
            {"reviews": [_dev_entry()], "tokenPagination": {"nextPageToken": None}}
        )
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert page.error is None
        assert page.next_cursor is None

    def test_a_non_string_token_is_a_parse_error(self):
        """``next_cursor`` is declared ``str | None`` and is sent back on the
        next request, so a number here is a malformed envelope, not a cursor."""
        body = json.dumps({"reviews": [], "tokenPagination": {"nextPageToken": 12345}})
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert page.error is not None
        assert page.error.kind == "parse"


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (401, "auth"),
            (403, "auth"),
            (404, "not_found"),
            (429, "rate_limited"),
            (500, "server"),
        ],
    )
    def test_status_maps_to_kind(self, status, kind):
        page = _serving("", status=status).fetch_page("com.example.app", "", None)

        assert page.error is not None
        assert page.error.kind == kind
        assert page.error.country is None

    def test_transport_failure_carries_the_real_message(self):
        def handler(request):
            raise httpx.ConnectError("no route to host")

        page = _provider(handler).fetch_page("com.example.app", "", None)

        assert page.error.kind == "transport"
        assert "no route to host" in page.error.message

    def test_malformed_json_is_a_parse_error(self):
        page = _serving("}{").fetch_page("com.example.app", "", None)

        assert page.error.kind == "parse"

    def test_null_body_is_a_parse_error(self):
        """`json.loads("null")` yields None; `.get()` on it must not escape
        as an uncaught AttributeError."""
        page = _serving("null").fetch_page("com.example.app", "", None)

        assert page.error is not None
        assert page.error.kind == "parse"
        assert page.error.retryable is False

    def test_non_list_reviews_is_a_parse_error(self):
        """Per-entry skipping must not soften the envelope contract: a
        ``reviews`` that isn't a list is a malformed response, not a page of
        entries that each happen to be unparseable."""
        page = _serving(json.dumps({"reviews": "abc"})).fetch_page(
            "com.example.app", "", None
        )

        assert page.error is not None
        assert page.error.kind == "parse"

    def test_non_dict_token_pagination_is_a_parse_error(self):
        """A ``tokenPagination`` value that isn't a dict (e.g.
        `{"tokenPagination": "next"}`) must yield FetchError(kind="parse"),
        not an uncaught AttributeError from `"next".get(...)`."""
        body = json.dumps({"reviews": [], "tokenPagination": "next"})
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert page.error is not None
        assert page.error.kind == "parse"


class TestEntryResilience:
    """One unparseable entry must not cost the rest of the page or the cursor.

    A page-level ``except`` around the whole entry mapping turned a single bad
    entry into a non-retryable ``parse`` error with no ``next_cursor``, which
    ends the walk in ``StopPolicy.evaluate`` and silently truncates the fetch.
    """

    def test_bad_entry_is_skipped_and_good_ones_survive(self):
        entries = [_dev_entry("good-1"), {"reviewId": "broken"}, _dev_entry("good-2")]
        page = _serving(_payload(entries)).fetch_page("com.example.app", "", None)

        assert page.error is None
        assert [r.id for r in page.reviews] == ["good-1", "good-2"]
        assert page.skipped_reviews == 1

    def test_bad_entry_preserves_the_cursor(self):
        entries = [{"reviewId": "broken"}, _dev_entry("good-1")]
        body = _payload(entries, token="NXT")
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert page.next_cursor == "NXT"

    def test_missing_star_rating_is_skipped_not_fatal(self):
        """``starRating`` absent defaulted to 0, which ``Review.__post_init__``
        rejects: a default that can never produce a valid Review."""
        entry = _dev_entry("no-rating")
        del entry["comments"][0]["userComment"]["starRating"]
        body = _payload([entry, _dev_entry("good-1")])
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert page.error is None
        assert [r.id for r in page.reviews] == ["good-1"]

    def test_a_wholly_unparseable_page_still_carries_its_cursor(self):
        body = _payload([{"reviewId": "a"}], token="NXT")
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert page.error is None
        assert not page.reviews
        assert page.next_cursor == "NXT"


class TestUnusableTimestamps:
    """A review whose timestamp cannot be read is skipped, never invented.

    ``updated_at`` is the only date this source reports, so ``sort``, ``since``
    and ``until`` all read it. A wrong one is worse than a missing review.
    """

    def test_a_review_with_no_last_modified_is_skipped(self):
        """``.get("lastModified", {})`` defaulted to seconds=0, dating an
        undated review to 1970-01-01 rather than dropping it."""
        entry = _dev_entry("undated")
        del entry["comments"][0]["userComment"]["lastModified"]
        body = _payload([entry, _dev_entry("good-1")])
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert [r.id for r in page.reviews] == ["good-1"]
        assert page.error is None

    def test_an_empty_last_modified_is_skipped(self):
        entry = _dev_entry("undated")
        entry["comments"][0]["userComment"]["lastModified"] = {}
        body = _payload([entry, _dev_entry("good-1")])
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert [r.id for r in page.reviews] == ["good-1"]

    @pytest.mark.parametrize(
        "seconds",
        [10**20, -(10**20), float("inf"), float("nan"), "yesterday", None],
    )
    def test_an_unusable_seconds_value_costs_only_that_review(self, seconds):
        """``fetch_page`` never raises; ``iter_pages`` is documented on that,
        and ``OverflowError`` from ``fromtimestamp`` is not a ``ValueError``."""
        body = _payload([_dev_entry("bad", seconds=seconds), _dev_entry("good-1")])
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert [r.id for r in page.reviews] == ["good-1"]
        assert page.error is None

    async def test_the_async_path_survives_it_too(self):
        body = _payload([_dev_entry("bad", seconds=10**20)])
        page = await _serving(body).afetch_page("com.example.app", "", None)

        assert page.reviews == []
        assert page.error is None


class TestSkippedEntryLogging:
    def test_the_reviewer_is_not_named_in_the_log(self, caplog):
        """An entry carries the author's name and the full review text."""
        entry = _dev_entry("rev-42", author="Carol Danvers", rating="not-a-number")
        entry["comments"][0]["userComment"]["text"] = "my private complaint"
        with caplog.at_level(logging.WARNING):
            _serving(_payload([entry])).fetch_page("com.example.app", "", None)

        assert "Carol Danvers" not in caplog.text
        assert "my private complaint" not in caplog.text

    def test_the_review_id_is_logged_so_the_warning_is_actionable(self, caplog):
        entry = _dev_entry("rev-42", rating="not-a-number")
        with caplog.at_level(logging.WARNING):
            _serving(_payload([entry])).fetch_page("com.example.app", "", None)

        assert "rev-42" in caplog.text


class TestAsyncParity:
    async def test_afetch_page_matches_fetch_page(self):
        body = _payload([_dev_entry()], token="N")
        sync_page = _serving(body).fetch_page("com.example.app", "", None)
        async_page = await _serving(body).afetch_page("com.example.app", "", None)

        assert [r.id for r in sync_page.reviews] == [r.id for r in async_page.reviews]
        assert sync_page.next_cursor == async_page.next_cursor


class TestAReviewWithADeveloperReply:
    """``comments`` holds a ``userComment`` and, once answered, a
    ``developerComment``. Indexing slot 0 works today but is order-dependent,
    the wrong order silently drops exactly the reviews a team has replied to."""

    def _replied(self):
        return {
            "reviewId": "answered",
            "authorName": "Dave",
            "comments": [
                {
                    "developerComment": {
                        "text": "Fixed in 5.1",
                        "lastModified": {"seconds": 1710600000},
                    }
                },
                {
                    "userComment": {
                        "text": "It crashes",
                        "starRating": 2,
                        "lastModified": {"seconds": 1710500000},
                    }
                },
            ],
        }

    def test_it_is_parsed_whichever_order_the_comments_arrive_in(self):
        body = _payload([self._replied(), _dev_entry("plain")])
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert sorted(r.id for r in page.reviews) == ["answered", "plain"]

    def test_the_user_comment_is_the_one_mapped(self):
        page = _serving(_payload([self._replied()])).fetch_page(
            "com.example.app", "", None
        )

        assert page.reviews[0].rating == 2
        assert page.reviews[0].body == "It crashes"

    def test_an_entry_with_no_user_comment_at_all_is_skipped(self):
        entry = {
            "reviewId": "reply-only",
            "comments": [
                {"developerComment": {"text": "hi", "lastModified": {"seconds": 1}}}
            ],
        }
        body = _payload([entry, _dev_entry("plain")])
        page = _serving(body).fetch_page("com.example.app", "", None)

        assert [r.id for r in page.reviews] == ["plain"]


class TestTheTimestampIsTheOneGoogleSent:
    """``lastModified`` is a protobuf Timestamp. Nothing asserted the resulting
    instant, so dividing the seconds by 1000 (every review dated 1970) or
    dropping ``tz=UTC`` both passed."""

    def test_seconds_become_that_instant_in_utc(self):
        entry = _dev_entry(seconds=1710500000)
        page = _serving(_payload([entry])).fetch_page("com.example.app", "", None)

        assert page.reviews[0].updated_at == datetime(
            2024, 3, 15, 10, 53, 20, tzinfo=UTC
        )

    def test_the_result_is_timezone_aware(self):
        """A naive datetime reaches ``Review`` and then raises ``TypeError`` on
        the first ``since`` comparison, out of a walk documented not to raise."""
        page = _serving(_payload([_dev_entry()])).fetch_page(
            "com.example.app", "", None
        )

        assert page.reviews[0].updated_at.tzinfo is not None
        assert page.reviews[0].dated_at > datetime(2020, 1, 1, tzinfo=UTC)
