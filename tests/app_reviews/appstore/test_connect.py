"""Tests for AppStoreOfficialProvider."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from app_reviews.appstore.connect import AppStoreOfficialProvider
from app_reviews.core.http import HttpClient
from tests.app_reviews.factories import StaticToken


def _connect_entry(
    review_id: str = "abc-123",
    rating: int = 4,
    title: str = "Nice",
    body: str = "Works well",
    nickname: str = "Bob",
    territory: str = "USA",
    created: str = "2024-05-01T12:00:00.000+00:00",
) -> dict[str, Any]:
    return {
        "id": review_id,
        "attributes": {
            "rating": rating,
            "title": title,
            "body": body,
            "reviewerNickname": nickname,
            "territory": territory,
            "createdDate": created,
        },
    }


def _payload(entries: list[dict[str, Any]], next_url: str | None = None) -> str:
    body: dict[str, Any] = {"data": entries}
    if next_url:
        body["links"] = {"next": next_url}
    return json.dumps(body)


def _provider(handler):
    """Build a provider whose pooled HTTP client uses a MockTransport."""
    return AppStoreOfficialProvider(
        StaticToken("Bearer token"),
        http=HttpClient(transport=httpx.MockTransport(handler)),
    )


class TestSource:
    def test_source(self):
        assert AppStoreOfficialProvider(StaticToken()).source == "appstore_official"


class TestFetchPage:
    def test_sends_authorization_header(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, text=_payload([_connect_entry()]))

        _provider(handler).fetch_page("12345", "", None)

        assert seen["auth"] == "Bearer token"

    def test_first_page_requests_newest_first(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_payload([_connect_entry()]))

        _provider(handler).fetch_page("12345", "", None)

        assert "sort=-createdDate" in seen["url"]
        assert "/apps/12345/customerReviews" in seen["url"]

    def test_cursor_is_used_as_the_next_url(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_payload([]))

        # The real shape, verified against the live API: Apple's links.next is
        # the same customerReviews path with a cursor param.
        _provider(handler).fetch_page(
            "12345",
            "",
            "https://api.appstoreconnect.apple.com"
            "/v1/apps/12345/customerReviews?cursor=X",
        )

        assert "cursor=X" in seen["url"]

    def test_maps_entry_fields(self):
        def handler(request):
            return httpx.Response(200, text=_payload([_connect_entry()]))

        page = _provider(handler).fetch_page("12345", "", None)
        review = page.reviews[0]

        assert review.id == "abc-123"
        assert review.app_id == "12345"
        assert review.rating == 4
        assert review.title == "Nice"
        assert review.body == "Works well"
        assert review.author_name == "Bob"
        assert review.country == "us"  # Connect sends "USA"; normalised
        assert review.source == "appstore_official"
        assert review.raw is not None

    def test_next_cursor_comes_from_links_next(self):
        def handler(request):
            return httpx.Response(
                200, text=_payload([_connect_entry()], next_url="https://next/page2")
            )

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.next_cursor == "https://next/page2"

    def test_missing_links_ends_pagination(self):
        def handler(request):
            return httpx.Response(200, text=_payload([_connect_entry()]))

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.next_cursor is None

    def test_missing_territory_is_none_not_empty_string(self):
        entry = _connect_entry()
        del entry["attributes"]["territory"]

        def handler(request):
            return httpx.Response(200, text=_payload([entry]))

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.reviews[0].country is None

    def test_empty_response_has_no_reviews_and_no_error(self):
        def handler(request):
            return httpx.Response(200, text=_payload([]))

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.reviews == []
        assert page.error is None
        assert page.next_cursor is None


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
        def handler(request):
            return httpx.Response(status, text="")

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.error is not None
        assert page.error.kind == kind
        assert page.error.country is None

    def test_auth_failure_is_not_retryable(self):
        def handler(request):
            return httpx.Response(401, text="")

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.error.retryable is False

    def test_transport_failure_carries_the_real_message(self):
        def handler(request):
            raise httpx.ConnectTimeout("timed out")

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.error.kind == "transport"
        assert "timed out" in page.error.message

    def test_malformed_json_is_a_parse_error(self):
        def handler(request):
            return httpx.Response(200, text="<html>nope</html>")

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.error.kind == "parse"

    def test_null_body_is_a_parse_error(self):
        def handler(request):
            return httpx.Response(200, text="null")

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.error is not None
        assert page.error.kind == "parse"
        assert page.error.retryable is False

    def test_non_list_data_is_a_parse_error(self):
        """Per-entry skipping must not soften the envelope contract: a ``data``
        that isn't a list is a malformed response, not a page of entries that
        each happen to be unparseable."""

        def handler(request):
            return httpx.Response(200, text=json.dumps({"data": "abc"}))

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.error is not None
        assert page.error.kind == "parse"

    def test_non_dict_links_is_a_parse_error(self):
        """A ``links`` value that isn't a dict (e.g. `{"links": "next"}`) must
        yield FetchError(kind="parse"), not an uncaught AttributeError from
        `"next".get(...)`, so the cursor extraction must sit inside the parse
        `try` alongside the entry mapping, not after it."""

        def handler(request):
            return httpx.Response(200, text=json.dumps({"data": [], "links": "next"}))

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.error is not None
        assert page.error.kind == "parse"


class TestEntryResilience:
    """One unparseable entry must not cost the rest of the page or the cursor.

    A page-level ``except`` around the whole entry mapping turned a single bad
    entry into a non-retryable ``parse`` error with no ``next_cursor``, which
    ends the walk in ``StopPolicy.evaluate`` and silently truncates the fetch.
    """

    def test_bad_entry_is_skipped_and_good_ones_survive(self):
        broken = _connect_entry("broken")
        broken["attributes"] = "not-a-dict"
        entries = [_connect_entry("good-1"), broken, _connect_entry("good-2")]

        def handler(request):
            return httpx.Response(200, text=_payload(entries))

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.error is None
        assert [r.id for r in page.reviews] == ["good-1", "good-2"]
        assert page.skipped_reviews == 1

    def test_bad_entry_preserves_the_cursor(self):
        broken = _connect_entry("broken")
        del broken["attributes"]["rating"]

        def handler(request):
            return httpx.Response(
                200,
                text=_payload([broken, _connect_entry("good-1")], next_url="https://n"),
            )

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.next_cursor == "https://n"
        assert [r.id for r in page.reviews] == ["good-1"]

    def test_unparseable_created_date_is_skipped_not_fatal(self):
        broken = _connect_entry("broken")
        broken["attributes"]["createdDate"] = "not-a-date"

        def handler(request):
            return httpx.Response(
                200, text=_payload([broken, _connect_entry("good-1")])
            )

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.error is None
        assert [r.id for r in page.reviews] == ["good-1"]

    def test_a_wholly_unparseable_page_still_carries_its_cursor(self):
        def handler(request):
            return httpx.Response(
                200, text=_payload([{"id": "a"}], next_url="https://n")
            )

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.error is None
        assert not page.reviews
        assert page.next_cursor == "https://n"

    def test_skipped_entry_is_logged(self, caplog):
        def handler(request):
            return httpx.Response(200, text=_payload([{"id": "broken"}]))

        with caplog.at_level(logging.WARNING):
            _provider(handler).fetch_page("12345", "", None)

        assert "Skipped review" in caplog.text


class TestAsyncParity:
    async def test_afetch_page_matches_fetch_page(self):
        def handler(request):
            return httpx.Response(
                200, text=_payload([_connect_entry()], next_url="https://next/p2")
            )

        sync_page = _provider(handler).fetch_page("12345", "", None)
        async_page = await _provider(handler).afetch_page("12345", "", None)

        assert [r.id for r in sync_page.reviews] == [r.id for r in async_page.reviews]
        assert sync_page.next_cursor == async_page.next_cursor

    async def test_afetch_page_classifies_errors_identically(self):
        def handler(request):
            return httpx.Response(429, text="")

        page = await _provider(handler).afetch_page("12345", "", None)

        assert page.error is not None
        assert page.error.kind == "rate_limited"


class TestTerritoryIsNormalised:
    """Connect reports the storefront as ISO alpha-3 (``"USA"``); the RSS feed and
    the ``Country`` enum use alpha-2 (``"us"``). Left as-is, the same field would
    carry two alphabets depending on which source produced the review, so
    grouping by country across sources split into separate buckets.
    """

    def test_alpha3_becomes_alpha2(self):
        entry = _connect_entry()
        entry["attributes"]["territory"] = "USA"

        def handler(_r):
            return httpx.Response(200, text=_payload([entry]))

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.reviews[0].country == "us"

    def test_it_compares_equal_to_the_country_enum(self):
        from app_reviews import Country

        entry = _connect_entry()
        entry["attributes"]["territory"] = "GBR"

        def handler(_r):
            return httpx.Response(200, text=_payload([entry]))

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.reviews[0].country == Country.GB

    def test_a_missing_territory_is_none(self):
        entry = _connect_entry()
        del entry["attributes"]["territory"]

        def handler(_r):
            return httpx.Response(200, text=_payload([entry]))

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.reviews[0].country is None

    def test_apples_raw_territory_is_still_reachable(self):
        entry = _connect_entry()
        entry["attributes"]["territory"] = "FRA"

        def handler(_r):
            return httpx.Response(200, text=_payload([entry]))

        page = _provider(handler).fetch_page("12345", "", None)

        assert page.reviews[0].raw["attributes"]["territory"] == "FRA"


class TestCursorIsNotAnOpenRedirect:
    """Connect returns a full URL in ``links.next`` and callers persist it, so by
    the time it comes back the cursor is untrusted input. Requests carry a signed
    JWT, so a cursor pointing elsewhere would hand that token to another host.
    """

    def _requests(self):
        seen = []

        def handler(request):
            seen.append((str(request.url), request.headers.get("authorization")))
            return httpx.Response(200, text=_payload([]))

        return seen, _provider(handler)

    @pytest.mark.parametrize(
        "cursor",
        [
            "https://evil.example.com/collect",
            "http://api.appstoreconnect.apple.com/v1/x",  # plain http
            "http://127.0.0.1:8080/x",
            "https://api.appstoreconnect.apple.com.evil.com/x",
            "file:///etc/passwd",
            "not-a-url",
            "",
        ],
    )
    def test_a_foreign_cursor_is_refused_without_a_request(self, cursor):
        seen, provider = self._requests()

        page = provider.fetch_page("12345", "", cursor)

        assert seen == [], "no request may be made"
        assert page.error is not None
        assert page.error.kind == "parse"
        assert page.error.retryable is False

    def test_the_token_never_leaves_apples_host(self):
        seen, provider = self._requests()

        provider.fetch_page("12345", "", "https://evil.example.com/collect")

        assert all("evil.example.com" not in url for url, _ in seen)

    def test_a_genuine_connect_cursor_is_followed(self):
        seen, provider = self._requests()
        cursor = (
            "https://api.appstoreconnect.apple.com/v1/apps/12345/customerReviews"
            "?cursor=NEXTPAGE&limit=200"
        )

        provider.fetch_page("12345", "", cursor)

        assert seen[-1][0] == cursor
        assert seen[-1][1] == "Bearer token"

    @pytest.mark.parametrize(
        "cursor",
        [
            "https://api.appstoreconnect.apple.com/v1/apps/other/customerReviews?cursor=X",
            "https://api.appstoreconnect.apple.com/v1/apps/12345/customerReviews/extra?cursor=X",
            "https://api.appstoreconnect.apple.com/v1/apps/12345/customerReviews/../users",
            "https://api.appstoreconnect.apple.com/v1/apps/12345/%2e%2e/users",
            "https://user@api.appstoreconnect.apple.com/v1/apps/12345/customerReviews",
            "https://api.appstoreconnect.apple.com/v1/apps/12345/customerReviews#secret",
        ],
    )
    def test_cursor_must_be_the_exact_collection_for_the_requested_app(self, cursor):
        seen, provider = self._requests()

        page = provider.fetch_page("12345", "", cursor)

        assert seen == []
        assert page.error is not None and page.error.kind == "parse"

    def test_rejection_happens_before_the_token_source_is_called(self):
        class ExplodingToken:
            def authorization_header(self):
                raise AssertionError("cursor validation must happen before JWT I/O")

            async def aauthorization_header(self):
                raise AssertionError("cursor validation must happen before JWT I/O")

        provider = AppStoreOfficialProvider(ExplodingToken())

        page = provider.fetch_page(
            "12345",
            "",
            "https://api.appstoreconnect.apple.com/v1/apps/other/customerReviews",
        )

        assert page.error is not None and page.error.kind == "parse"

    @pytest.mark.parametrize("port", ["abc", "70000"])
    def test_malformed_port_is_a_parse_result_before_token_or_http(self, port):
        class ExplodingToken:
            def authorization_header(self):
                raise AssertionError("token source must not be called")

            async def aauthorization_header(self):
                raise AssertionError("token source must not be called")

        provider = AppStoreOfficialProvider(ExplodingToken())
        cursor = (
            f"https://api.appstoreconnect.apple.com:{port}"
            "/v1/apps/12345/customerReviews"
        )

        page = provider.fetch_page("12345", "", cursor)

        assert page.error is not None and page.error.kind == "parse"

    @pytest.mark.parametrize("port", ["abc", "70000"])
    async def test_async_malformed_port_is_a_parse_result_before_token_or_http(
        self, port
    ):
        class ExplodingToken:
            def authorization_header(self):
                raise AssertionError("token source must not be called")

            async def aauthorization_header(self):
                raise AssertionError("token source must not be called")

        provider = AppStoreOfficialProvider(ExplodingToken())
        cursor = (
            f"https://api.appstoreconnect.apple.com:{port}"
            "/v1/apps/12345/customerReviews"
        )

        page = await provider.afetch_page("12345", "", cursor)

        assert page.error is not None and page.error.kind == "parse"

    async def test_the_async_path_refuses_too(self):
        seen, provider = self._requests()

        page = await provider.afetch_page("12345", "", "https://evil.example.com/x")

        assert seen == []
        assert page.error is not None and page.error.kind == "parse"

    def test_no_cursor_uses_the_first_page_url(self):
        seen, provider = self._requests()

        provider.fetch_page("12345", "", None)

        assert "api.appstoreconnect.apple.com" in seen[-1][0]
        assert "sort=-createdDate" in seen[-1][0]


class TestConnectFieldResilience:
    """Only ``id``, ``rating`` and ``createdDate`` can cost a review."""

    def _one(self, mutate):
        entry = _connect_entry()
        mutate(entry)

        def handler(_r):
            return httpx.Response(200, text=_payload([entry]))

        return _provider(handler).fetch_page("12345", "", None)

    def test_an_empty_id_drops_the_review(self):
        """It keys the documented `(store, source, id)` deduplication."""
        page = self._one(lambda e: e.update(id=""))

        assert page.reviews == []
        assert page.error is None

    def test_an_empty_title_is_none_not_blank(self):
        page = self._one(lambda e: e["attributes"].update(title=""))

        assert page.reviews[0].title is None

    def test_a_missing_body_keeps_the_review(self):
        page = self._one(lambda e: e["attributes"].pop("body"))

        assert len(page.reviews) == 1
        assert page.reviews[0].body == ""

    def test_a_missing_nickname_keeps_the_review(self):
        page = self._one(lambda e: e["attributes"].pop("reviewerNickname"))

        assert len(page.reviews) == 1
        assert page.reviews[0].author_name == ""

    @pytest.mark.parametrize("rating", [True, 4.5, "5"])
    def test_a_coercible_non_integer_rating_is_skipped(self, rating):
        page = self._one(lambda e: e["attributes"].update(rating=rating))

        assert page.reviews == []
        assert page.skipped_reviews == 1

    @pytest.mark.parametrize("rating", [1, 3, 5])
    def test_a_real_integer_rating_is_kept(self, rating):
        page = self._one(lambda e: e["attributes"].update(rating=rating))

        assert [review.rating for review in page.reviews] == [rating]
        assert page.skipped_reviews == 0

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda e: e["attributes"].pop("rating"),
            lambda e: e["attributes"].update(rating=0),
            lambda e: e["attributes"].pop("createdDate"),
            lambda e: e["attributes"].update(createdDate="not-a-date"),
        ],
    )
    def test_an_unusable_required_field_drops_only_that_review(self, mutate):
        page = self._one(mutate)

        assert page.reviews == []
        assert page.error is None

    def test_a_dropped_review_does_not_cost_its_neighbours(self, caplog):
        import logging

        broken = _connect_entry("broken")
        broken["attributes"].pop("rating")

        def handler(_r):
            return httpx.Response(
                200,
                text=_payload(
                    [_connect_entry("a"), broken, _connect_entry("b")],
                    next_url="https://api.appstoreconnect.apple.com/v1/next",
                ),
            )

        with caplog.at_level(logging.WARNING):
            page = _provider(handler).fetch_page("12345", "", None)

        assert [r.id for r in page.reviews] == ["a", "b"]
        assert page.next_cursor is not None
        assert "Skipped review" in caplog.text


class TestSkippedReviewLogging:
    """A parse failure must not write the reviewer or their words to logs.

    Fixed first in the Google providers; this one carried the same line.
    """

    def _body_with_a_bad_entry(self):
        return json.dumps(
            {
                "data": [
                    {
                        "id": "rev-42",
                        "attributes": {
                            "rating": "not-a-number",
                            "createdDate": "2026-01-01T00:00:00-07:00",
                            "title": "My private complaint",
                            "body": "the body text",
                            "reviewerNickname": "Carol Danvers",
                            "territory": "USA",
                        },
                    }
                ]
            }
        )

    def test_the_reviewer_and_their_words_are_not_logged(self, caplog):
        def handler(request):
            return httpx.Response(200, text=self._body_with_a_bad_entry())

        with caplog.at_level(logging.WARNING):
            _provider(handler).fetch_page("123", "", None)

        assert "Carol Danvers" not in caplog.text
        assert "My private complaint" not in caplog.text
        assert "the body text" not in caplog.text

    def test_the_review_id_is_logged_so_the_warning_is_actionable(self, caplog):
        def handler(request):
            return httpx.Response(200, text=self._body_with_a_bad_entry())

        with caplog.at_level(logging.WARNING):
            _provider(handler).fetch_page("123", "", None)

        assert "rev-42" in caplog.text


class TestAppIdIsNotAPathInjection:
    """``app_id`` lands in the URL path of a request carrying a signed JWT.

    Unescaped, ``..`` segments in it let a caller retarget that request at other
    paths on the Connect API, because httpx normalises the traversal away before
    sending. Same fix as ``googleplay/developer_api.py``.
    """

    def _url_for(self, app_id):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=json.dumps({"data": []}))

        _provider(handler).fetch_page(app_id, "", None)
        return seen["url"]

    def test_a_normal_app_id_is_untouched(self):
        assert "/v1/apps/389801252/customerReviews" in self._url_for("389801252")

    @pytest.mark.parametrize(
        "app_id",
        [
            "../../../../v1/apps/999/customerReviews",
            "a/../../../evil",
            "389801252/../../other",
        ],
    )
    def test_traversal_cannot_leave_the_reviews_endpoint(self, app_id):
        url = self._url_for(app_id)

        assert url.startswith("https://api.appstoreconnect.apple.com/v1/apps/")
        assert "/customerReviews?" in url
        assert "/v1/apps/999" not in url
        assert "/evil" not in url


class TestAnUnusableCursorIsReportedNotRaised:
    """The cursor is persisted by callers, so by the time it comes back it is
    untrusted input. Every rejection has to arrive as page data, since ``iter_pages``
    documents that it does not raise."""

    @pytest.mark.parametrize(
        ("cursor", "why"),
        [
            ("https://[::1", "a malformed IPv6 literal makes urlsplit raise"),
            ("https://api.appstoreconnect.apple.com]:443/x", "unbalanced brackets"),
            (
                "https://api.appstoreconnect.apple.com:8443/v1/apps/1/customerReviews",
                "a non-standard port",
            ),
            ("https://api.appstoreconnect.apple.com/v1/users", "another endpoint"),
            (
                "https://api.appstoreconnect.apple.com/v1/apps/1/x\nCRITICAL forged",
                "a control character that could forge a log record",
            ),
        ],
    )
    def test_it_becomes_a_parse_error(self, cursor, why):
        page = _provider(lambda r: httpx.Response(200, text="{}")).fetch_page(
            "1", "", cursor
        )

        assert page.error is not None, why
        assert page.error.kind == "parse", why

    def test_the_walk_does_not_raise(self):
        from app_reviews.core.reviews import BaseReviews

        class _Client(BaseReviews):
            def _build_provider(self):
                return _provider(lambda r: httpx.Response(200, text="{}"))

        pages = list(_Client().iter_pages("1", cursor="https://[::1"))

        assert pages[-1].error.kind == "parse"

    def test_a_real_next_link_still_works(self):
        good = (
            "https://api.appstoreconnect.apple.com/v1/apps/1/customerReviews?cursor=abc"
        )
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=json.dumps({"data": []}))

        _provider(handler).fetch_page("1", "", good)

        assert seen["url"] == good


class TestANonStringNextLinkIsAParseError:
    """``links.next`` goes straight back out as the next cursor, so a value that
    is not a string is a malformed envelope, and an unhashable one broke the
    walk's cycle detection with an uncaught ``TypeError``."""

    @pytest.mark.parametrize(
        "value", [{"href": "https://x/"}, ["https://x/"], 12345, True]
    )
    def test_it_is_reported_rather_than_returned(self, value):
        body = json.dumps({"data": [], "links": {"next": value}})
        page = _provider(lambda r: httpx.Response(200, text=body)).fetch_page(
            "1", "", None
        )

        assert page.error is not None
        assert page.error.kind == "parse"

    def test_a_null_next_link_is_simply_the_last_page(self):
        body = json.dumps({"data": [], "links": {"next": None}})
        page = _provider(lambda r: httpx.Response(200, text=body)).fetch_page(
            "1", "", None
        )

        assert page.error is None
        assert page.next_cursor is None


class TestTheTimestampIsTheOneAppleSent:
    def test_the_offset_is_preserved(self):
        entry = {
            "id": "r1",
            "attributes": {
                "rating": 4,
                "createdDate": "2026-01-15T09:30:00-07:00",
                "territory": "USA",
            },
        }
        body = json.dumps({"data": [entry]})
        page = _provider(lambda r: httpx.Response(200, text=body)).fetch_page(
            "1", "", None
        )

        assert page.reviews[0].created_at == datetime(
            2026, 1, 15, 9, 30, tzinfo=timezone(timedelta(hours=-7))
        )
        assert page.reviews[0].updated_at is None
