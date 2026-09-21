"""Tests for AppStoreScraperProvider."""

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from app_reviews.appstore.rss import AppStoreScraperProvider
from app_reviews.core.http import HttpClient
from app_reviews.core.reviews import BaseReviews


def _rss_entry(
    review_id: str = "111",
    rating: str = "5",
    title: str = "Great app",
    body: str = "Love it",
    author: str = "Alice",
    version: str = "1.0",
    updated: str = "2024-03-15T10:00:00-07:00",
) -> dict[str, Any]:
    return {
        "id": {"label": review_id},
        "im:rating": {"label": rating},
        "title": {"label": title},
        "content": {"label": body},
        "author": {"name": {"label": author}},
        "im:version": {"label": version},
        "updated": {"label": updated},
    }


def _feed(entries: list[dict[str, Any]]) -> str:
    return json.dumps({"feed": {"entry": entries}} if entries else {"feed": {}})


def _provider(handler):
    """Build a provider whose pooled HTTP client uses a MockTransport."""
    return AppStoreScraperProvider(
        http=HttpClient(transport=httpx.MockTransport(handler))
    )


class TestSource:
    def test_source(self):
        assert AppStoreScraperProvider().source == "appstore_scraper"


class TestFetchPage:
    def test_first_page_uses_page_1(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_feed([_rss_entry()]))

        _provider(handler).fetch_page("12345", "us", None)

        assert "/page=1/" in seen["url"]

    def test_cursor_sets_page_number(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_feed([_rss_entry()]))

        _provider(handler).fetch_page("12345", "us", "4")

        assert "/page=4/" in seen["url"]

    def test_maps_entry_fields(self):
        def handler(request):
            return httpx.Response(200, text=_feed([_rss_entry()]))

        page = _provider(handler).fetch_page("12345", "us", None)
        review = page.reviews[0]

        assert review.id == "111"
        assert review.rating == 5
        assert review.title == "Great app"
        assert review.body == "Love it"
        assert review.author_name == "Alice"
        assert review.country == "us"
        assert review.source == "appstore_scraper"
        assert review.raw == _rss_entry()

    def test_next_cursor_advances_while_entries_returned(self):
        def handler(request):
            return httpx.Response(200, text=_feed([_rss_entry()]))

        page = _provider(handler).fetch_page("12345", "us", None)

        assert page.next_cursor == "2"

    def test_empty_feed_ends_pagination(self):
        def handler(request):
            return httpx.Response(200, text=_feed([]))

        page = _provider(handler).fetch_page("12345", "us", None)

        assert page.reviews == []
        assert page.error is None
        assert page.next_cursor is None

    def test_stops_past_max_pages_without_a_request(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, text=_feed([_rss_entry()]))

        page = _provider(handler).fetch_page("12345", "us", "11")

        assert calls["n"] == 0
        assert page.next_cursor is None

    def test_page_ten_has_no_next_cursor(self):
        """The 10th page must not advertise an 11th, because that page would only
        ever short-circuit to an empty PageResult, overcounting `pages` and
        yielding a trailing empty page from a walking consumer."""

        def handler(request):
            return httpx.Response(200, text=_feed([_rss_entry()]))

        page = _provider(handler).fetch_page("12345", "us", "10")

        assert page.reviews != []
        assert page.next_cursor is None

    def test_full_depth_walk_yields_exactly_ten_pages(self):
        """A page-driving consumer walking every page from a source that
        never runs dry within the 10-page depth cap must see exactly 10
        pages, not an 11th trailing empty one."""

        def handler(request):
            return httpx.Response(200, text=_feed([_rss_entry()]))

        provider = _provider(handler)
        pages = []
        cursor = None
        while True:
            page = provider.fetch_page("12345", "us", cursor)
            pages.append(page)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert len(pages) == 10
        assert all(p.reviews for p in pages)

    def test_full_depth_walk_reports_ten_pages_not_eleven(self):
        """Client-level view of the same fix: `CountryOutcome.pages` must
        count the 10 real requests, not an 11th trailing empty one."""

        class _ScraperOnlyClient(BaseReviews):
            def __init__(self, provider):
                super().__init__()
                self._provider = provider

            def _build_provider(self):
                return self._provider

        def handler(request):
            return httpx.Response(200, text=_feed([_rss_entry()]))

        result = _ScraperOnlyClient(_provider(handler)).fetch("12345", countries=["us"])

        assert result.outcomes[0].pages == 10
        assert result.outcomes[0].stopped_because == "exhausted"


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("status", "kind", "retryable"),
        [
            (429, "rate_limited", True),
            (403, "request", False),
            (404, "not_found", False),
            (503, "server", True),
        ],
    )
    def test_status_maps_to_kind(self, status, kind, retryable):
        def handler(request):
            return httpx.Response(status, text="")

        page = _provider(handler).fetch_page("12345", "us", None)

        assert page.error is not None
        assert page.error.kind == kind
        assert page.error.status == status
        assert page.error.retryable is retryable
        assert page.error.country == "us"

    def test_transport_failure_carries_the_real_message(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        page = _provider(handler).fetch_page("12345", "us", None)

        assert page.error is not None
        assert page.error.kind == "transport"
        assert "connection refused" in page.error.message
        assert "HTTP 0" not in page.error.message

    def test_public_feed_403_explains_access_blocking_not_credentials(self):
        page = _provider(lambda request: httpx.Response(403, text="")).fetch_page(
            "12345", "us", None
        )

        assert page.error is not None
        assert page.error.kind == "request"
        assert "access may be blocked or throttled" in page.error.message

    def test_malformed_json_is_a_parse_error(self):
        def handler(request):
            return httpx.Response(200, text="not json at all")

        page = _provider(handler).fetch_page("12345", "us", None)

        assert page.error is not None
        assert page.error.kind == "parse"

    def test_null_feed_body_is_a_parse_error(self):
        def handler(request):
            return httpx.Response(200, text="null")

        page = _provider(handler).fetch_page("12345", "us", None)

        assert page.error is not None
        assert page.error.kind == "parse"
        assert page.error.retryable is False

    def test_a_non_dict_version_field_costs_only_the_field(self):
        """``app_version`` is optional, so a wrong shape gives None; it does not
        cost the review, and it certainly does not cost the page."""
        odd = _rss_entry("odd")
        odd["im:version"] = "1.0"

        def handler(request):
            return httpx.Response(200, text=_feed([odd, _rss_entry("good")]))

        page = _provider(handler).fetch_page("12345", "us", None)

        assert page.error is None
        assert [r.id for r in page.reviews] == ["odd", "good"]
        assert page.reviews[0].app_version is None


class TestAsyncParity:
    async def test_afetch_page_matches_fetch_page(self):
        def handler(request):
            return httpx.Response(200, text=_feed([_rss_entry()]))

        sync_page = _provider(handler).fetch_page("12345", "us", None)
        async_page = await _provider(handler).afetch_page("12345", "us", None)

        assert [r.id for r in sync_page.reviews] == [r.id for r in async_page.reviews]
        assert sync_page.next_cursor == async_page.next_cursor

    async def test_afetch_page_classifies_errors_identically(self):
        def handler(request):
            return httpx.Response(429, text="")

        page = await _provider(handler).afetch_page("12345", "us", None)

        assert page.error is not None
        assert page.error.kind == "rate_limited"


class TestLabelReader:
    """Every value in the feed is wrapped as ``{"label": ...}``."""

    def _read(self, entry, *path):
        return AppStoreScraperProvider()._label(entry, *path)

    def test_reads_a_label(self):
        assert self._read({"title": {"label": "Great"}}, "title") == "Great"

    def test_walks_a_nested_path(self):
        entry = {"author": {"name": {"label": "Alice"}}}
        assert self._read(entry, "author", "name") == "Alice"

    def test_strips_whitespace(self):
        assert self._read({"t": {"label": "  hello  "}}, "t") == "hello"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("a\r\nb", "a\nb"), ("a\rb", "a\nb"), ("a\r\nb\rc\nd", "a\nb\nc\nd")],
    )
    def test_normalises_line_endings(self, raw, expected):
        assert self._read({"t": {"label": raw}}, "t") == expected

    @pytest.mark.parametrize("blank", ["", "   ", "  \n\r\n  "])
    def test_blank_is_none_not_empty_string(self, blank):
        """The package uses None for "not reported"; the feed uses ""."""
        assert self._read({"t": {"label": blank}}, "t") is None

    def test_a_missing_key_is_none(self):
        assert self._read({}, "title") is None

    def test_a_missing_label_is_none(self):
        assert self._read({"title": {}}, "title") is None

    def test_an_unexpected_shape_is_none(self):
        assert self._read({"title": "Great"}, "title") is None
        assert self._read({"title": ["Great"]}, "title") is None

    def test_a_non_string_label_is_none(self):
        assert self._read({"t": {"label": 5}}, "t") is None


class TestEntryResilience:
    """One unparseable entry must not cost the page or the cursor.

    ``_parse_entries`` pre-filtered on two fields, so anything else malformed
    (a missing title, an unreadable date) escaped into the page-level handler,
    which discards ``next_cursor`` and ends the walk. Same shape as the fix in
    both official providers.
    """

    def test_a_bad_entry_is_skipped_and_the_rest_survive(self):
        broken = _rss_entry("broken")
        del broken["updated"]

        def handler(_r):
            return httpx.Response(
                200, text=_feed([_rss_entry("a"), broken, _rss_entry("b")])
            )

        page = _provider(handler).fetch_page("12345", "us", None)

        assert page.error is None
        assert [r.id for r in page.reviews] == ["a", "b"]

    def test_a_bad_entry_preserves_the_cursor(self):
        broken = _rss_entry("broken")
        broken["updated"] = {"label": "not-a-date"}

        def handler(_r):
            return httpx.Response(200, text=_feed([broken, _rss_entry("a")]))

        page = _provider(handler).fetch_page("12345", "us", None)

        assert page.next_cursor == "2"
        assert [r.id for r in page.reviews] == ["a"]

    def test_skipped_entries_are_logged(self, caplog):
        """Dropped rows must leave a trace."""
        import logging

        def handler(_r):
            return httpx.Response(200, text=_feed([{"id": {"label": "x"}}]))

        with caplog.at_level(logging.WARNING):
            _provider(handler).fetch_page("12345", "us", None)

        assert "Skipped review" in caplog.text


class TestSingleEntryQuirk:
    """Apple returns ``feed.entry`` as a bare object, not a one-element list,
    when an app has exactly one review. Iterating that dict yields its *keys*,
    so every row was silently discarded and the app looked reviewless.
    """

    def test_one_review_arrives_as_an_object(self):
        def handler(_r):
            return httpx.Response(
                200, text=json.dumps({"feed": {"entry": _rss_entry("solo")}})
            )

        page = _provider(handler).fetch_page("12345", "us", None)

        assert [r.id for r in page.reviews] == ["solo"]
        assert page.error is None

    def test_a_list_still_works(self):
        def handler(_r):
            return httpx.Response(200, text=_feed([_rss_entry("a"), _rss_entry("b")]))

        page = _provider(handler).fetch_page("12345", "us", None)

        assert len(page.reviews) == 2

    def test_a_nonsense_entry_value_is_a_parse_error(self):
        """Neither a list nor a dict is a malformed envelope, not empty data."""

        def handler(_r):
            return httpx.Response(200, text=json.dumps({"feed": {"entry": "oops"}}))

        page = _provider(handler).fetch_page("12345", "us", None)

        assert page.error is not None
        assert page.error.kind == "parse"


class TestCursorRobustness:
    """Rung 1 exists so a cursor can be persisted and resumed in another process.
    A corrupted checkpoint must come back as a ``FetchError``, not an exception,
    ``iter_pages`` promises it never raises.
    """

    @pytest.mark.parametrize("cursor", ["abc", "", "1.5", "-3", "0", "  ", "1e2"])
    def test_an_unusable_cursor_becomes_an_error_not_an_exception(self, cursor):
        def handler(_r):
            return httpx.Response(200, text=_feed([_rss_entry()]))

        page = _provider(handler).fetch_page("12345", "us", cursor)

        assert page.error is not None
        assert page.error.kind == "parse"
        assert page.error.retryable is False

    def test_a_bad_cursor_never_reaches_the_network(self):
        calls = []

        def handler(r):
            calls.append(1)
            return httpx.Response(200, text=_feed([_rss_entry()]))

        _provider(handler).fetch_page("12345", "us", "abc")

        assert calls == []

    async def test_the_async_path_behaves_the_same(self):
        def handler(_r):
            return httpx.Response(200, text=_feed([_rss_entry()]))

        page = await _provider(handler).afetch_page("12345", "us", "abc")

        assert page.error is not None and page.error.kind == "parse"

    def test_past_the_depth_cap_is_still_a_clean_stop(self):
        """Not an error: the feed genuinely has no page 11."""

        def handler(_r):
            return httpx.Response(200, text=_feed([_rss_entry()]))

        page = _provider(handler).fetch_page("12345", "us", "11")

        assert page.error is None
        assert page.reviews == []
        assert page.next_cursor is None


class TestFieldLevelResilience:
    """A missing *optional* field must not cost the review.

    Every value in the feed is wrapped as ``{"label": ...}``. A missing key, a
    wrong shape, or an empty string all mean "not reported", so they become
    ``None``, not a dropped review and not an empty string.
    """

    def _one(self, mutate):
        entry = _rss_entry()
        mutate(entry)

        def handler(_r):
            return httpx.Response(200, text=_feed([entry]))

        return _provider(handler).fetch_page("12345", "us", None)

    def test_a_missing_title_keeps_the_review(self):
        page = self._one(lambda e: e.pop("title"))

        assert len(page.reviews) == 1
        assert page.reviews[0].title is None

    def test_an_empty_title_is_none_not_blank(self):
        page = self._one(lambda e: e["title"].update(label=""))

        assert page.reviews[0].title is None

    def test_a_wrongly_shaped_title_keeps_the_review(self):
        page = self._one(lambda e: e.update(title="Great"))

        assert len(page.reviews) == 1
        assert page.reviews[0].title is None

    def test_a_missing_author_keeps_the_review(self):
        page = self._one(lambda e: e.pop("author"))

        assert len(page.reviews) == 1
        assert page.reviews[0].author_name == ""

    def test_an_empty_version_is_none(self):
        page = self._one(lambda e: e["im:version"].update(label=""))

        assert page.reviews[0].app_version is None

    def test_an_empty_body_stays_an_empty_string(self):
        """``body`` is not optional: a review with nothing written is still a
        review, and "" is the honest value for it."""
        page = self._one(lambda e: e["content"].update(label=""))

        assert page.reviews[0].body == ""

    @pytest.mark.parametrize(
        ("field", "mutate"),
        [
            ("id", lambda e: e["id"].update(label="")),
            ("rating", lambda e: e["im:rating"].update(label="")),
            ("rating", lambda e: e["im:rating"].update(label="0")),
            ("updated", lambda e: e.pop("updated")),
            ("updated", lambda e: e["updated"].update(label="not-a-date")),
        ],
    )
    def test_an_unusable_required_field_drops_the_review(self, field, mutate):
        """``id`` keys deduplication, ``rating`` and ``updated`` cannot be faked."""
        page = self._one(mutate)

        assert page.reviews == []
        assert page.error is None  # the page survives; only the entry is lost
        assert page.skipped_reviews == 1

    def test_a_dropped_review_does_not_cost_its_neighbours(self):
        broken = _rss_entry("broken")
        broken["id"].update(label="")

        def handler(_r):
            return httpx.Response(
                200, text=_feed([_rss_entry("a"), broken, _rss_entry("b")])
            )

        page = _provider(handler).fetch_page("12345", "us", None)

        assert [r.id for r in page.reviews] == ["a", "b"]
        assert page.skipped_reviews == 1
        assert page.next_cursor == "2"


class TestSkippedReviewLogging:
    """A parse failure must not write the reviewer or their words to logs.

    Fixed first in the Google providers; the App Store ones had the same line.
    """

    def _page_with_a_bad_entry(self):
        entry = {
            "id": {"label": "rev-42"},
            "im:rating": {"label": "not-a-number"},
            "updated": {"label": "2026-01-01T00:00:00-07:00"},
            "title": {"label": "My private complaint"},
            "content": {"label": "the body text"},
            "author": {"name": {"label": "Carol Danvers"}},
        }
        return json.dumps({"feed": {"entry": [entry]}})

    def test_the_reviewer_and_their_words_are_not_logged(self, caplog):
        import logging

        def handler(request):
            return httpx.Response(200, text=self._page_with_a_bad_entry())

        with caplog.at_level(logging.WARNING):
            _provider(handler).fetch_page("123", "us", None)

        assert "Carol Danvers" not in caplog.text
        assert "My private complaint" not in caplog.text
        assert "the body text" not in caplog.text

    def test_the_review_id_is_logged_so_the_warning_is_actionable(self, caplog):
        import logging

        def handler(request):
            return httpx.Response(200, text=self._page_with_a_bad_entry())

        with caplog.at_level(logging.WARNING):
            _provider(handler).fetch_page("123", "us", None)

        assert "rev-42" in caplog.text


class TestPathSegmentsAreNotInjectable:
    """``app_id`` and ``country`` both land in the RSS URL path.

    No credential rides on this request, so the stake is a silently wrong URL
    rather than a leaked token, but a request that quietly goes somewhere else
    is still a request whose empty result means nothing.
    """

    def _url_for(self, app_id, country):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=json.dumps({"feed": {"entry": []}}))

        _provider(handler).fetch_page(app_id, country, None)
        return seen["url"]

    def test_a_normal_request_is_untouched(self):
        url = self._url_for("389801252", "us")

        assert "/us/rss/customerreviews/id=389801252/" in url

    @pytest.mark.parametrize(
        ("app_id", "country"),
        [
            ("../../../../evil", "us"),
            ("389801252", "../../x"),
            ("a/../b", "g/../h"),
        ],
    )
    def test_traversal_cannot_leave_the_feed_path(self, app_id, country):
        url = self._url_for(app_id, country)

        assert "/rss/customerreviews/id=" in url
        assert url.endswith("/json")
        assert "/evil" not in url


class TestAPageOfUnusableEntriesDoesNotEndTheWalk:
    """``next_cursor`` was gated on the *parsed* reviews, so a page whose entries
    all failed to map reported ``exhausted`` ("there is no more data") and the
    next page was never requested."""

    def _walk(self):
        bad = {"id": {"label": "x"}, "updated": {"label": "2026-01-01T00:00:00-07:00"}}
        good = {
            "id": {"label": "g"},
            "im:rating": {"label": "5"},
            "updated": {"label": "2026-01-01T00:00:00-07:00"},
        }
        bodies = [
            json.dumps({"feed": {"entry": [bad, bad]}}),
            json.dumps({"feed": {"entry": [good]}}),
            json.dumps({"feed": {"entry": []}}),
        ]
        seen = {"n": 0}

        def handler(request):
            i = seen["n"]
            seen["n"] += 1
            return httpx.Response(200, text=bodies[min(i, 2)])

        return _provider(handler)

    def test_the_page_still_offers_a_next_cursor(self):
        page = self._walk().fetch_page("1", "us", None)

        assert page.reviews == []
        assert page.next_cursor == "2"

    def test_a_genuinely_empty_feed_still_ends_the_walk(self):
        provider = _provider(
            lambda r: httpx.Response(200, text=json.dumps({"feed": {"entry": []}}))
        )

        assert provider.fetch_page("1", "us", None).next_cursor is None


class TestTheTimestampIsTheOneApplesSent:
    """Nothing asserted a parsed date in any provider, so a mutation as small as
    ``.replace(tzinfo=UTC)``, which shifts every review by the feed's offset,
    passed the whole suite. A ``since`` boundary silently moves with it."""

    def _entry(self, updated):
        return {
            "id": {"label": "r1"},
            "im:rating": {"label": "5"},
            "updated": {"label": updated},
        }

    def test_the_offset_is_preserved_not_reinterpreted(self):
        body = json.dumps(
            {"feed": {"entry": [self._entry("2026-01-15T09:30:00-07:00")]}}
        )
        page = _provider(lambda r: httpx.Response(200, text=body)).fetch_page(
            "1", "us", None
        )

        assert page.reviews[0].updated_at == datetime(
            2026, 1, 15, 9, 30, tzinfo=timezone(timedelta(hours=-7))
        )
        assert page.reviews[0].updated_at.astimezone(UTC).hour == 16

    def test_it_lands_on_updated_at_not_created_at(self):
        """Atom's ``updated`` is last-modified; there is no creation date."""
        body = json.dumps(
            {"feed": {"entry": [self._entry("2026-01-15T09:30:00-07:00")]}}
        )
        page = _provider(lambda r: httpx.Response(200, text=body)).fetch_page(
            "1", "us", None
        )

        assert page.reviews[0].created_at is None
        assert page.reviews[0].dated_at == page.reviews[0].updated_at


class TestTheAsyncWalkRequestsTheSamePage:
    """The async path computing ``page + 1`` passed the suite, because no async test
    ever inspected the URL, so the walk skipped page 1 while still reporting
    cursor "2"."""

    async def test_the_first_async_page_is_page_one(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=json.dumps({"feed": {"entry": []}}))

        await _provider(handler).afetch_page("123", "us", None)

        assert "/page=1/" in seen["url"]

    async def test_an_async_cursor_requests_that_page(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=json.dumps({"feed": {"entry": []}}))

        await _provider(handler).afetch_page("123", "us", "4")

        assert "/page=4/" in seen["url"]

    async def test_the_async_url_matches_the_sync_one(self):
        urls = []

        def handler(request):
            urls.append(str(request.url))
            return httpx.Response(200, text=json.dumps({"feed": {"entry": []}}))

        _provider(handler).fetch_page("123", "us", "3")
        await _provider(handler).afetch_page("123", "us", "3")

        assert urls[0] == urls[1]


class TestTheStorefrontIsUsedVerbatimInLowercase:
    """RSS stamps ``country`` on the review from the caller's argument while
    Connect normalises, so ``countries=["US"]`` produced ``"US"`` from one
    source and ``"us"`` from the other."""

    def test_an_uppercase_country_is_normalised_on_the_review(self):
        entry = {
            "id": {"label": "r1"},
            "im:rating": {"label": "5"},
            "updated": {"label": "2026-01-15T09:30:00-07:00"},
        }
        body = json.dumps({"feed": {"entry": [entry]}})
        page = _provider(lambda r: httpx.Response(200, text=body)).fetch_page(
            "1", "US", None
        )

        assert page.reviews[0].country == "us"
