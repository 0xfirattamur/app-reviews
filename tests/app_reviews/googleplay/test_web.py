"""Tests for GooglePlayScraperProvider."""

import json
import logging
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app_reviews.core.http import HttpClient
from app_reviews.core.paging import is_per_country, orders_newest_first
from app_reviews.googleplay.web import GooglePlayScraperProvider


def _gp_entry(
    review_id: str = "abc123",
    author: str = "Carol",
    rating: Any = 5,
    body: str = "Excellent",
    created_ts: Any = 1710500000,
    nanos: int | None = None,
    app_version: str = "2.1.0",
) -> list[Any]:
    entry: list[Any] = [None] * 11
    entry[0] = review_id
    entry[1] = [author]
    entry[2] = rating
    entry[4] = body
    entry[5] = [created_ts, nanos] if nanos is not None else [created_ts]
    entry[10] = app_version
    return entry


def _gp_body(entries: list[list[Any]], token: str | None = None) -> str:
    """Build a batchexecute response envelope.

    The live payload is three slots (reviews, a pagination pair holding the
    token, and a trailing marker), so mirror that or the token lands elsewhere.
    """
    inner = json.dumps([entries, [None, token] if token else None, [None]])
    return ")]}'\n\n" + json.dumps([["wrb.fr", "oCPfdb", inner, None, "generic"]])


def _provider(handler, **kwargs):
    """Build a provider whose pooled HTTP client uses a MockTransport."""
    return GooglePlayScraperProvider(
        http=HttpClient(transport=httpx.MockTransport(handler)), **kwargs
    )


def _serving(text: str, status: int = 200):
    return _provider(lambda request: httpx.Response(status, text=text))


def _sent_payload(body: str) -> Any:
    """The decoded RPC argument out of a urlencoded ``f.req`` body."""
    f_req = urllib.parse.parse_qs(body)["f.req"][0]
    return json.loads(json.loads(f_req)[0][0][1])


class TestSource:
    def test_source(self):
        assert GooglePlayScraperProvider().source == "googleplay_scraper"

    def test_is_not_per_country(self):
        """Play has one global review corpus, so there is only ever one call to
        make. That fact lives solely in ``core.paging``; see the reasoning on
        ``_PER_COUNTRY`` there.
        """
        assert is_per_country(GooglePlayScraperProvider().source) is False


class TestRequestShape:
    def test_country_shapes_the_storefront_param(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_gp_body([_gp_entry()]))

        _provider(handler).fetch_page("com.example.app", "de", None)

        assert "gl=de" in seen["url"]

    def test_language_is_always_english(self):
        """``hl`` picks the store's presentation locale, not a review filter."""
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_gp_body([_gp_entry()]))

        _provider(handler).fetch_page("com.example.app", "de", None)

        assert "hl=en" in seen["url"]

    def test_empty_country_falls_back_to_us(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_gp_body([_gp_entry()]))

        _provider(handler).fetch_page("com.example.app", "", None)

        assert "gl=us" in seen["url"]

    def test_cursor_is_sent_as_the_page_token(self):
        seen = {}

        def handler(request):
            seen["body"] = request.content.decode()
            return httpx.Response(200, text=_gp_body([_gp_entry()]))

        _provider(handler).fetch_page("com.example.app", "us", "TOKEN123")

        assert "TOKEN123" in seen["body"]


class TestSortOrder:
    """Newest-first is a fixed property of this source, not a knob.

    ``core.paging.orders_newest_first`` is True for it, and the ``since``
    walk stops early on that promise, and a different sort would truncate those
    walks silently, so the request must always ask for newest.
    """

    def test_requests_are_always_sorted_newest_first(self):
        seen = {}

        def handler(request):
            seen["body"] = request.content.decode()
            return httpx.Response(200, text=_gp_body([_gp_entry()]))

        _provider(handler).fetch_page("com.example.app", "us", None)

        assert _sent_payload(seen["body"])[1][1] == (
            GooglePlayScraperProvider.SORT_NEWEST
        )

    def test_capabilities_agrees_that_this_source_is_newest_first(self):
        assert orders_newest_first("googleplay_scraper") is True


class TestFieldMapping:
    def test_maps_entry_fields(self):
        page = _serving(_gp_body([_gp_entry()])).fetch_page(
            "com.example.app", "us", None
        )
        review = page.reviews[0]

        assert review.id == "abc123"
        assert review.rating == 5
        assert review.body == "Excellent"
        assert review.author_name == "Carol"
        assert review.app_version == "2.1.0"
        assert review.source == "googleplay_scraper"

    def test_country_is_none_even_when_a_storefront_was_queried(self):
        """`de` still shapes the request's `gl` param (see TestRequestShape),
        but Play has no country dimension to report back on the review
        itself: there is one global corpus, not a `de` slice of it."""
        page = _serving(_gp_body([_gp_entry()])).fetch_page(
            "com.example.app", "de", None
        )

        assert page.reviews[0].country is None

    def test_country_is_none_when_storefront_defaulted(self):
        page = _serving(_gp_body([_gp_entry()])).fetch_page("com.example.app", "", None)

        assert page.reviews[0].country is None

    def test_title_is_none_because_google_play_has_no_titles(self):
        page = _serving(_gp_body([_gp_entry()])).fetch_page(
            "com.example.app", "us", None
        )

        assert page.reviews[0].title is None

    def test_nanos_become_sub_second_precision(self):
        entry = _gp_entry(created_ts=1710500000, nanos=839_000_000)
        page = _serving(_gp_body([entry])).fetch_page("com.example.app", "us", None)

        assert page.reviews[0].created_at.microsecond == 839_000

    def test_raw_holds_the_source_entry_unwrapped(self):
        """``raw`` is what the source sent, in the same shape as every other
        provider, so reading it must not require branching on the source."""
        entry = _gp_entry()
        page = _serving(_gp_body([entry])).fetch_page("com.example.app", "us", None)

        assert page.reviews[0].raw == entry

    def test_unparseable_entry_is_skipped_not_fatal(self):
        body = _gp_body([["broken"], _gp_entry()])
        page = _serving(body).fetch_page("com.example.app", "us", None)

        assert len(page.reviews) == 1
        assert page.error is None
        assert page.skipped_reviews == 1


class TestUnusableTimestamps:
    """``fetch_page`` never raises; ``iter_pages`` is documented on that."""

    @pytest.mark.parametrize(
        ("seconds", "label"),
        [
            (10**20, "out of range for the platform clock"),
            (-(10**20), "negative and out of range"),
            (float("nan"), "not a number"),
            (float("inf"), "infinite"),
            ("yesterday", "not a number at all"),
        ],
    )
    def test_an_unusable_timestamp_costs_only_that_review(self, seconds, label):
        body = _gp_body([_gp_entry(review_id="bad", created_ts=seconds), _gp_entry()])
        page = _serving(body).fetch_page("com.example.app", "us", None)

        assert [r.id for r in page.reviews] == ["abc123"], label
        assert page.error is None

    async def test_the_async_path_survives_it_too(self):
        body = _gp_body([_gp_entry(review_id="bad", created_ts=10**20)])
        page = await _serving(body).afetch_page("com.example.app", "us", None)

        assert page.reviews == []
        assert page.error is None


class TestSkippedReviewLogging:
    def test_the_reviewer_is_not_named_in_the_log(self, caplog):
        """A parse failure must not write a reviewer's name or photo to logs."""
        entry = _gp_entry(author="Carol Danvers", rating="not-a-number")
        with caplog.at_level(logging.WARNING):
            _serving(_gp_body([entry])).fetch_page("com.example.app", "us", None)

        assert "Carol Danvers" not in caplog.text

    def test_the_review_id_is_logged_so_the_warning_is_actionable(self, caplog):
        entry = _gp_entry(review_id="rev-42", rating="not-a-number")
        with caplog.at_level(logging.WARNING):
            _serving(_gp_body([entry])).fetch_page("com.example.app", "us", None)

        assert "rev-42" in caplog.text


class TestPagination:
    def test_next_cursor_comes_from_the_token(self):
        body = _gp_body([_gp_entry()], token="NEXT")
        page = _serving(body).fetch_page("com.example.app", "us", None)

        assert page.next_cursor == "NEXT"

    def test_missing_token_ends_pagination(self):
        page = _serving(_gp_body([_gp_entry()])).fetch_page(
            "com.example.app", "us", None
        )

        assert page.next_cursor is None


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("status", "kind"),
        [(429, "rate_limited"), (403, "auth"), (404, "not_found"), (503, "server")],
    )
    def test_status_maps_to_kind(self, status, kind):
        page = _serving("", status=status).fetch_page("com.example.app", "us", None)

        assert page.error is not None
        assert page.error.kind == kind
        # Global source: no country to attribute the failure to, matching both
        # official providers.
        assert page.error.country is None

    def test_transport_failure_carries_the_real_message(self):
        def handler(request):
            raise httpx.ReadTimeout("read timed out")

        page = _provider(handler).fetch_page("com.example.app", "us", None)

        assert page.error.kind == "transport"
        assert "read timed out" in page.error.message

    def test_missing_response_prefix_is_a_parse_error(self):
        page = _serving("garbage without the prefix").fetch_page(
            "com.example.app", "us", None
        )

        assert page.error is not None
        assert page.error.kind == "parse"

    def test_null_body_without_prefix_is_a_parse_error(self):
        """A bare "null" body never matches the batchexecute prefix regex."""
        page = _serving("null").fetch_page("com.example.app", "us", None)

        assert page.error is not None
        assert page.error.kind == "parse"
        assert page.error.retryable is False

    def test_null_inner_payload_with_prefix_is_a_parse_error(self):
        """The prefix and envelope wrapper are present, but the RPC payload
        itself parses to ``null`` instead of the expected array, so `len(data)`
        on that ``None`` must not escape as an uncaught TypeError."""
        raw = ")]}'\n\n" + json.dumps([["wrb.fr", "oCPfdb", "null", None, "generic"]])
        page = _serving(raw).fetch_page("com.example.app", "us", None)

        assert page.error is not None
        assert page.error.kind == "parse"
        assert page.error.retryable is False

    def test_no_matching_envelope_is_a_parse_error(self):
        """The prefix is present and the outer array parses, but it contains
        no ``wrb.fr``/``oCPfdb`` item. This must surface as a parse error,
        not a silently empty page."""
        page = _serving(")]}'\n\n[]").fetch_page("com.example.app", "us", None)

        assert page.error is not None
        assert page.error.kind == "parse"


class TestAsyncParity:
    async def test_afetch_page_matches_fetch_page(self):
        body = _gp_body([_gp_entry()], token="N")
        sync_page = _serving(body).fetch_page("com.example.app", "us", None)
        async_page = await _serving(body).afetch_page("com.example.app", "us", None)

        assert [r.id for r in sync_page.reviews] == [r.id for r in async_page.reviews]
        assert sync_page.next_cursor == async_page.next_cursor
        assert sync_page.reviews[0].country == async_page.reviews[0].country


class TestTheStorefrontIsFullyEscaped:
    """The one URL-building site the escaping sweep missed: ``quote`` defaults to
    ``safe="/"``, so a slash in the country survived into the query string."""

    def test_a_slash_in_the_country_is_escaped(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_gp_body([_gp_entry()]))

        _provider(handler).fetch_page("com.example.app", "../../us/rss/x", None)

        assert "gl=..%2F..%2Fus%2Frss%2Fx" in seen["url"]
        assert "/rss/" not in seen["url"]


class TestTheTimestampIsTheOneGoogleSent:
    def test_seconds_become_that_instant_in_utc(self):
        entry = _gp_entry(created_ts=1710500000)
        page = _serving(_gp_body([entry])).fetch_page("com.example.app", "us", None)

        assert page.reviews[0].created_at == datetime(
            2024, 3, 15, 10, 53, 20, tzinfo=UTC
        )

    def test_it_lands_on_created_at_not_updated_at(self):
        page = _serving(_gp_body([_gp_entry()])).fetch_page(
            "com.example.app", "us", None
        )

        assert page.reviews[0].updated_at is None
        assert page.reviews[0].dated_at == page.reviews[0].created_at


class TestTheRequestNamesTheAppAskedlFor:
    """Nothing checked which app the RPC argument asked for, while ``_review``
    stamps ``app_id`` from the caller's argument, so another app's reviews would
    come back confidently labelled with yours."""

    def test_the_app_id_is_in_the_rpc_argument(self):
        seen = {}

        def handler(request):
            seen["body"] = request.content.decode()
            return httpx.Response(200, text=_gp_body([_gp_entry()]))

        _provider(handler).fetch_page("com.example.app", "us", None)

        assert _sent_payload(seen["body"])[2][0] == "com.example.app"

    def test_the_cursor_lands_in_the_pagination_slot(self):
        """A substring check on the urlencoded blob passes even when the cursor
        is put in the wrong slot: Play then re-serves page 1 forever."""
        seen = {}

        def handler(request):
            seen["body"] = request.content.decode()
            return httpx.Response(200, text=_gp_body([_gp_entry()]))

        _provider(handler).fetch_page("com.example.app", "us", "TOKEN123")

        assert _sent_payload(seen["body"])[1][2][2] == "TOKEN123"


class TestTheSortConstantIsThePlayValueForNewest:
    """Asserting the provider sends its own constant cannot catch the constant
    being wrong. Play's sort ids: 1 = most relevant, 2 = newest."""

    def test_newest_is_two(self):
        assert GooglePlayScraperProvider.SORT_NEWEST == 2
