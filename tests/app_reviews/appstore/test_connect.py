"""Tests for AppStoreOfficialProvider."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from app_reviews.appstore.connect import AppStoreOfficialProvider
from tests.app_reviews.factories import StaticToken
from tests.app_reviews.transport import Recorder, client_for, query_of


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
    return client_for(
        AppStoreOfficialProvider, handler, auth=StaticToken("Bearer token")
    )


class TestSource:
    def test_source(self):
        assert AppStoreOfficialProvider(StaticToken()).source == "appstore_official"


class TestFetchPage:
    def test_sends_authorization_header(self):
        record = Recorder(httpx.Response(200, text=_payload([_connect_entry()])))

        _provider(record).fetch_page("12345", "", None)

        assert record.last.headers.get("authorization") == "Bearer token"

    def test_first_page_requests_newest_first(self):
        record = Recorder(httpx.Response(200, text=_payload([_connect_entry()])))

        _provider(record).fetch_page("12345", "", None)

        assert query_of(record.last)["sort"] == ["-createdDate"]
        assert "/apps/12345/customerReviews" in record.last.url.path

    def test_cursor_is_used_as_the_next_url(self):
        record = Recorder(httpx.Response(200, text=_payload([])))

        # The real shape, verified against the live API: Apple's links.next is
        # the same customerReviews path with a cursor param.
        _provider(record).fetch_page(
            "12345",
            "",
            "https://api.appstoreconnect.apple.com"
            "/v1/apps/12345/customerReviews?cursor=X",
        )

        assert query_of(record.last)["cursor"] == ["X"]

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
    async def test_status_maps_to_kind(self, twin, status, kind):
        def handler(request):
            return httpx.Response(status, text="")

        page = await twin.call(_provider(handler), "fetch_page", "12345", "", None)

        assert page.error is not None
        assert page.error.kind == kind
        assert page.error.country is None

    async def test_auth_failure_is_not_retryable(self, twin):
        def handler(request):
            return httpx.Response(401, text="")

        page = await twin.call(_provider(handler), "fetch_page", "12345", "", None)

        assert page.error.retryable is False

    async def test_transport_failure_carries_the_real_message(self, twin):
        def handler(request):
            raise httpx.ConnectTimeout("timed out")

        page = await twin.call(_provider(handler), "fetch_page", "12345", "", None)

        assert page.error.kind == "transport"
        assert "timed out" in page.error.message

    async def test_malformed_json_is_a_parse_error(self, twin):
        def handler(request):
            return httpx.Response(200, text="<html>nope</html>")

        page = await twin.call(_provider(handler), "fetch_page", "12345", "", None)

        assert page.error.kind == "parse"

    async def test_null_body_is_a_parse_error(self, twin):
        def handler(request):
            return httpx.Response(200, text="null")

        page = await twin.call(_provider(handler), "fetch_page", "12345", "", None)

        assert page.error is not None
        assert page.error.kind == "parse"
        assert page.error.retryable is False

    async def test_non_list_data_is_a_parse_error(self, twin):
        """Per-entry skipping must not soften the envelope contract: a ``data``
        that isn't a list is a malformed response, not a page of entries that
        each happen to be unparseable."""

        def handler(request):
            return httpx.Response(200, text=json.dumps({"data": "abc"}))

        page = await twin.call(_provider(handler), "fetch_page", "12345", "", None)

        assert page.error is not None
        assert page.error.kind == "parse"

    async def test_non_dict_links_is_a_parse_error(self, twin):
        """A ``links`` value that isn't a dict (e.g. `{"links": "next"}`) must
        yield FetchError(kind="parse"), not an uncaught AttributeError from
        `"next".get(...)`, so the cursor extraction must sit inside the parse
        `try` alongside the entry mapping, not after it."""

        def handler(request):
            return httpx.Response(200, text=json.dumps({"data": [], "links": "next"}))

        page = await twin.call(_provider(handler), "fetch_page", "12345", "", None)

        assert page.error is not None
        assert page.error.kind == "parse"


class TestEntryResilience:
    """One unparseable entry must not cost the rest of the page or the cursor.

    A page-level ``except`` around the whole entry mapping turned a single bad
    entry into a non-retryable ``parse`` error with no ``next_cursor``, which
    ends the walk in ``StopPolicy.evaluate`` and silently truncates the fetch.
    """

    async def test_bad_entry_is_skipped_and_good_ones_survive(self, twin):
        broken = _connect_entry("broken")
        broken["attributes"] = "not-a-dict"
        entries = [_connect_entry("good-1"), broken, _connect_entry("good-2")]

        def handler(request):
            return httpx.Response(200, text=_payload(entries))

        page = await twin.call(_provider(handler), "fetch_page", "12345", "", None)

        assert page.error is None
        assert [r.id for r in page.reviews] == ["good-1", "good-2"]
        assert page.skipped_reviews == 1

    async def test_bad_entry_preserves_the_cursor(self, twin):
        broken = _connect_entry("broken")
        del broken["attributes"]["rating"]

        def handler(request):
            return httpx.Response(
                200,
                text=_payload([broken, _connect_entry("good-1")], next_url="https://n"),
            )

        page = await twin.call(_provider(handler), "fetch_page", "12345", "", None)

        assert page.next_cursor == "https://n"
        assert [r.id for r in page.reviews] == ["good-1"]

    async def test_unparseable_created_date_is_skipped_not_fatal(self, twin):
        broken = _connect_entry("broken")
        broken["attributes"]["createdDate"] = "not-a-date"

        def handler(request):
            return httpx.Response(
                200, text=_payload([broken, _connect_entry("good-1")])
            )

        page = await twin.call(_provider(handler), "fetch_page", "12345", "", None)

        assert page.error is None
        assert [r.id for r in page.reviews] == ["good-1"]

    async def test_a_wholly_unparseable_page_still_carries_its_cursor(self, twin):
        def handler(request):
            return httpx.Response(
                200, text=_payload([{"id": "a"}], next_url="https://n")
            )

        page = await twin.call(_provider(handler), "fetch_page", "12345", "", None)

        assert page.error is None
        assert not page.reviews
        assert page.next_cursor == "https://n"

    async def test_skipped_entry_is_logged(self, twin, caplog):
        def handler(request):
            return httpx.Response(200, text=_payload([{"id": "broken"}]))

        with caplog.at_level(logging.WARNING):
            await twin.call(_provider(handler), "fetch_page", "12345", "", None)

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
        record = Recorder(httpx.Response(200, text=_payload([])))
        return record, _provider(record)

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
    async def test_a_foreign_cursor_is_refused_without_a_request(self, twin, cursor):
        record, provider = self._requests()

        page = await twin.call(provider, "fetch_page", "12345", "", cursor)

        assert record.requests == [], "no request may be made"
        assert page.error is not None
        assert page.error.kind == "parse"
        assert page.error.retryable is False

    def test_the_token_never_leaves_apples_host(self):
        record, provider = self._requests()

        provider.fetch_page("12345", "", "https://evil.example.com/collect")

        assert all(
            request.url.host != "evil.example.com" for request in record.requests
        )

    def test_a_genuine_connect_cursor_is_followed(self):
        record, provider = self._requests()
        cursor = (
            "https://api.appstoreconnect.apple.com/v1/apps/12345/customerReviews"
            "?cursor=NEXTPAGE&limit=200"
        )

        provider.fetch_page("12345", "", cursor)

        # The cursor is followed as Apple sent it, asking for replies too.
        assert str(record.last.url) == f"{cursor}&include=response"
        assert record.last.headers["authorization"] == "Bearer token"

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
    async def test_cursor_must_be_the_exact_collection_for_the_requested_app(
        self, twin, cursor
    ):
        record, provider = self._requests()

        page = await twin.call(provider, "fetch_page", "12345", "", cursor)

        assert record.requests == []
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
    async def test_malformed_port_is_a_parse_result_before_token_or_http(
        self, twin, port
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

        page = await twin.call(provider, "fetch_page", "12345", "", cursor)

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
        record, provider = self._requests()

        page = await provider.afetch_page("12345", "", "https://evil.example.com/x")

        assert record.requests == []
        assert page.error is not None and page.error.kind == "parse"

    def test_no_cursor_uses_the_first_page_url(self):
        record, provider = self._requests()

        provider.fetch_page("12345", "", None)

        assert record.last.url.host == "api.appstoreconnect.apple.com"
        assert query_of(record.last)["sort"] == ["-createdDate"]


class TestConnectFieldResilience:
    """Only ``id``, ``rating`` and ``createdDate`` can cost a review."""

    async def _one(self, twin, mutate):
        entry = _connect_entry()
        mutate(entry)

        def handler(_r):
            return httpx.Response(200, text=_payload([entry]))

        return await twin.call(_provider(handler), "fetch_page", "12345", "", None)

    async def test_an_empty_id_drops_the_review(self, twin):
        """It keys the documented `(store, source, id)` deduplication."""
        page = await self._one(twin, lambda e: e.update(id=""))

        assert page.reviews == []
        assert page.error is None

    async def test_an_empty_title_is_none_not_blank(self, twin):
        page = await self._one(twin, lambda e: e["attributes"].update(title=""))

        assert page.reviews[0].title is None

    async def test_a_missing_body_keeps_the_review(self, twin):
        page = await self._one(twin, lambda e: e["attributes"].pop("body"))

        assert len(page.reviews) == 1
        assert page.reviews[0].body == ""

    async def test_a_missing_nickname_keeps_the_review(self, twin):
        page = await self._one(twin, lambda e: e["attributes"].pop("reviewerNickname"))

        assert len(page.reviews) == 1
        assert page.reviews[0].author_name == ""

    @pytest.mark.parametrize("rating", [True, 4.5, "5"])
    async def test_a_coercible_non_integer_rating_is_skipped(self, twin, rating):
        page = await self._one(twin, lambda e: e["attributes"].update(rating=rating))

        assert page.reviews == []
        assert page.skipped_reviews == 1

    @pytest.mark.parametrize("rating", [1, 3, 5])
    async def test_a_real_integer_rating_is_kept(self, twin, rating):
        page = await self._one(twin, lambda e: e["attributes"].update(rating=rating))

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
    async def test_an_unusable_required_field_drops_only_that_review(
        self, twin, mutate
    ):
        page = await self._one(twin, mutate)

        assert page.reviews == []
        assert page.error is None

    async def test_a_dropped_review_does_not_cost_its_neighbours(self, twin, caplog):
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
            page = await twin.call(_provider(handler), "fetch_page", "12345", "", None)

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
        record = Recorder(httpx.Response(200, text=json.dumps({"data": []})))

        _provider(record).fetch_page(app_id, "", None)
        return str(record.last.url)

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
    async def test_it_becomes_a_parse_error(self, twin, cursor, why):
        page = await twin.call(
            _provider(lambda r: httpx.Response(200, text="{}")),
            "fetch_page",
            "1",
            "",
            cursor,
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

    async def test_a_real_next_link_still_works(self, twin):
        good = (
            "https://api.appstoreconnect.apple.com/v1/apps/1/customerReviews?cursor=abc"
        )
        record = Recorder(httpx.Response(200, text=json.dumps({"data": []})))

        await twin.call(_provider(record), "fetch_page", "1", "", good)

        assert str(record.last.url) == f"{good}&include=response"


class TestANonStringNextLinkIsAParseError:
    """``links.next`` goes straight back out as the next cursor, so a value that
    is not a string is a malformed envelope, and an unhashable one broke the
    walk's cycle detection with an uncaught ``TypeError``."""

    @pytest.mark.parametrize(
        "value", [{"href": "https://x/"}, ["https://x/"], 12345, True]
    )
    async def test_it_is_reported_rather_than_returned(self, twin, value):
        body = json.dumps({"data": [], "links": {"next": value}})
        page = await twin.call(
            _provider(lambda r: httpx.Response(200, text=body)),
            "fetch_page",
            "1",
            "",
            None,
        )

        assert page.error is not None
        assert page.error.kind == "parse"

    async def test_a_null_next_link_is_simply_the_last_page(self, twin):
        body = json.dumps({"data": [], "links": {"next": None}})
        page = await twin.call(
            _provider(lambda r: httpx.Response(200, text=body)),
            "fetch_page",
            "1",
            "",
            None,
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


def _replied_entry(review_id: str, response_id: str) -> dict[str, Any]:
    entry = _connect_entry(review_id=review_id)
    entry["relationships"] = {
        "response": {"data": {"type": "customerReviewResponses", "id": response_id}}
    }
    return entry


def _response(response_id: str, text: str, state: str = "PUBLISHED") -> dict:
    return {
        "type": "customerReviewResponses",
        "id": response_id,
        "attributes": {
            "responseBody": text,
            "state": state,
            "lastModifiedDate": "2024-05-02T08:00:00.000+00:00",
        },
    }


class TestDeveloperRepliesComeWithTheReviews:
    """``include=response`` puts replies in ``included``; each review names its
    reply by id under ``relationships.response``."""

    def _page(self, body: dict[str, Any]):
        provider = _provider(lambda _r: httpx.Response(200, text=json.dumps(body)))
        return provider.fetch_page("12345", "", None)

    def test_the_first_page_asks_for_replies(self):
        record = Recorder(httpx.Response(200, text=_payload([])))

        _provider(record).fetch_page("12345", "", None)

        assert record.last.url.params["include"] == "response"

    def test_a_cursor_that_already_asks_for_replies_is_unchanged(self):
        record = Recorder(httpx.Response(200, text=_payload([])))
        cursor = (
            "https://api.appstoreconnect.apple.com/v1/apps/12345/customerReviews"
            "?cursor=N&include=response"
        )

        _provider(record).fetch_page("12345", "", cursor)

        assert str(record.last.url) == cursor

    def test_replies_are_joined_by_id_not_by_position(self):
        page = self._page(
            {
                "data": [_replied_entry("r1", "a"), _replied_entry("r2", "b")],
                "included": [
                    _response("b", "Second", state="PENDING_PUBLISH"),
                    {"type": "somethingElse", "id": "a"},
                    _response("a", "First"),
                ],
            }
        )

        replies = {r.id: r.developer_reply for r in page.reviews}
        assert replies["r1"].text == "First"
        assert replies["r1"].reply_id == "a"
        assert replies["r1"].state == "published"
        assert replies["r2"].text == "Second"
        assert replies["r2"].state == "pending"

    def test_a_review_without_a_reply_has_none(self):
        page = self._page({"data": [_connect_entry()]})

        assert page.reviews[0].developer_reply is None

    @pytest.mark.parametrize(
        "included",
        [
            [_response("a", "Hi", state="SOMETHING_NEW")],
            [{"type": "customerReviewResponses", "id": "a"}],
            "not-a-list",
            [],
        ],
    )
    def test_an_unusable_reply_costs_only_the_reply(self, included):
        page = self._page(
            {
                "data": [_replied_entry("r1", "a")],
                "included": included,
                "links": {"next": "https://api.appstoreconnect.apple.com/x"},
            }
        )

        assert [r.id for r in page.reviews] == ["r1"]
        assert page.reviews[0].developer_reply is None
        assert page.skipped_reviews == 0
        assert page.next_cursor is not None

    async def test_the_async_path_joins_them_too(self):
        body = {"data": [_replied_entry("r1", "a")], "included": [_response("a", "Hi")]}
        provider = _provider(lambda _r: httpx.Response(200, text=json.dumps(body)))

        page = await provider.afetch_page("12345", "", None)

        assert page.reviews[0].developer_reply.text == "Hi"
