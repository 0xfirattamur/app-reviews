"""Tests for the public page-level API: rungs 1 and 2 of the client ladder."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest

from app_reviews.core.paging import (
    _NEWEST_FIRST,
    _PER_COUNTRY,
    DEFAULT_MAX_EMPTY_PAGES,
    StopPolicy,
    is_per_country,
    orders_newest_first,
    prefer_stop_reason,
)
from app_reviews.core.reviews import BaseReviews
from app_reviews.errors import AuthError, ServerError, TransportError
from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError
from app_reviews.models.review import Review
from app_reviews.models.types import Sort, Source


@pytest.mark.parametrize("safety_reason", ["cycle", "stalled", "max_pages"])
def test_limit_outranks_safety_floor_stop_reasons(safety_reason):
    assert prefer_stop_reason(safety_reason, "limit") == "limit"


@pytest.mark.parametrize("higher_reason", ["error", "exhausted", "since"])
def test_limit_preserves_higher_priority_stop_reasons(higher_reason):
    assert prefer_stop_reason(higher_reason, "limit") == higher_reason


def _review(created_at: datetime, review_id: str = "1", rating: int = 5) -> Review:
    return Review(
        store="appstore",
        app_id="123",
        country="us",
        rating=rating,
        title="t",
        body="b",
        author_name="a",
        created_at=created_at,
        source="appstore_scraper",
        id=review_id,
    )


class FakeProvider:
    """A scripted provider. Records every (country, cursor) it was asked for.

    ``calls`` records every request regardless of entry point, so existing
    assertions on call counts hold for both sync and async walks. ``async_calls``
    records only requests that went through ``afetch_page``, so a test can
    prove the async path actually used it instead of silently falling back to
    the sync ``fetch_page`` body.
    """

    def __init__(self, pages, source="appstore_scraper"):
        self._pages = pages
        self.source = source
        self.calls: list[tuple[str, str | None]] = []
        self.async_calls: list[tuple[str, str | None]] = []

    def fetch_page(self, app_id, country, cursor):
        self.calls.append((country, cursor))
        return self._page_for(cursor)

    async def afetch_page(self, app_id, country, cursor):
        self.calls.append((country, cursor))
        self.async_calls.append((country, cursor))
        return self._page_for(cursor)

    def _page_for(self, cursor):
        index = 0 if cursor is None else int(cursor)
        if index >= len(self._pages):
            return PageResult()
        return self._pages[index]


class FakeClient(BaseReviews):
    def __init__(self, provider):
        super().__init__()
        self._provider = provider

    def _build_provider(self):
        return self._provider


def _page(reviews, next_cursor):
    return PageResult(reviews=reviews, next_cursor=next_cursor)


NOW = datetime(2026, 7, 30, tzinfo=UTC)


class TestFetchPage:
    def test_returns_a_single_page_without_paging(self):
        provider = FakeProvider(
            [_page([_review(NOW)], "1"), _page([_review(NOW)], None)]
        )
        client = FakeClient(provider)

        page = client.fetch_page("123", country="us")

        assert len(page.reviews) == 1
        assert page.next_cursor == "1"
        assert len(provider.calls) == 1

    def test_passes_the_cursor_through(self):
        provider = FakeProvider([_page([], "1"), _page([_review(NOW)], None)])
        client = FakeClient(provider)

        client.fetch_page("123", country="us", cursor="1")

        assert provider.calls == [("us", "1")]

    def test_never_sets_a_stop_reason(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        page = FakeClient(provider).fetch_page("123", country="us")

        assert page.stopped_because is None

    def test_country_defaults_for_a_global_provider(self):
        provider = FakeProvider([_page([], None)], source="googleplay_official")

        FakeClient(provider).fetch_page("123")

        assert provider.calls == [("", None)]


class TestIterPages:
    def test_walks_until_the_cursor_is_exhausted(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                _page([_review(NOW, "b")], "2"),
                _page([_review(NOW, "c")], None),
            ]
        )
        pages = list(FakeClient(provider).iter_pages("123", country="us"))

        assert len(pages) == 3
        assert [r.id for p in pages for r in p.reviews] == ["a", "b", "c"]

    def test_marks_the_final_page_exhausted(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        pages = list(FakeClient(provider).iter_pages("123", country="us"))

        assert pages[-1].stopped_because == "exhausted"

    def test_intermediate_pages_have_no_stop_reason(self):
        provider = FakeProvider(
            [_page([_review(NOW)], "1"), _page([_review(NOW)], None)]
        )
        pages = list(FakeClient(provider).iter_pages("123", country="us"))

        assert pages[0].stopped_because is None
        assert pages[1].stopped_because == "exhausted"

    def test_resumes_from_a_supplied_cursor(self):
        provider = FakeProvider([_page([], "1"), _page([_review(NOW, "b")], None)])
        pages = list(FakeClient(provider).iter_pages("123", country="us", cursor="1"))

        assert [r.id for p in pages for r in p.reviews] == ["b"]
        assert provider.calls[0] == ("us", "1")


class TestSinceEarlyStop:
    def test_stops_once_a_page_predates_since(self):
        old = NOW - timedelta(days=30)
        provider = FakeProvider(
            [
                _page([_review(NOW, "new")], "1"),
                _page([_review(old, "old")], "2"),
                _page([_review(old, "older")], "3"),
            ]
        )
        client = FakeClient(provider)

        pages = list(
            client.iter_pages("123", country="us", since=NOW - timedelta(days=2))
        )

        assert len(pages) == 2
        assert pages[-1].stopped_because == "since"
        assert len(provider.calls) == 2  # the third page is never requested

    def test_does_not_stop_while_pages_are_within_the_window(self):
        provider = FakeProvider(
            [_page([_review(NOW, "a")], "1"), _page([_review(NOW, "b")], None)]
        )
        client = FakeClient(provider)

        pages = list(
            client.iter_pages("123", country="us", since=NOW - timedelta(days=2))
        )

        assert len(pages) == 2
        assert pages[-1].stopped_because == "exhausted"

    def test_accepts_a_plain_date(self):
        old = NOW - timedelta(days=30)
        provider = FakeProvider(
            [_page([_review(old, "old")], "1"), _page([_review(old)], "2")]
        )
        client = FakeClient(provider)

        pages = list(client.iter_pages("123", country="us", since=NOW.date()))

        assert pages[-1].stopped_because == "since"

    def test_never_early_stops_when_ordering_is_not_guaranteed(self):
        old = NOW - timedelta(days=30)
        provider = FakeProvider(
            [
                _page([_review(old, "old")], "1"),
                _page([_review(NOW, "new")], None),
            ],
            source="googleplay_official",
        )
        client = FakeClient(provider)

        pages = list(client.iter_pages("123", since=NOW - timedelta(days=2)))

        assert len(pages) == 2
        assert pages[-1].stopped_because == "exhausted"


class TestLimit:
    def test_stops_once_limit_reviews_are_yielded(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a"), _review(NOW, "b")], "1"),
                _page([_review(NOW, "c")], "2"),
            ]
        )
        pages = list(FakeClient(provider).iter_pages("123", country="us", limit=2))

        assert len(pages) == 1
        assert pages[-1].stopped_because == "limit"
        assert len(provider.calls) == 1

    def test_accumulates_across_pages(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                _page([_review(NOW, "b")], "2"),
                _page([_review(NOW, "c")], "3"),
            ]
        )
        pages = list(FakeClient(provider).iter_pages("123", country="us", limit=2))

        assert len(pages) == 2
        assert pages[-1].stopped_because == "limit"

    def test_exhausted_outranks_limit(self):
        """No more data must never be reported as 'we stopped asking'."""
        provider = FakeProvider([_page([_review(NOW, "a"), _review(NOW, "b")], None)])

        pages = list(FakeClient(provider).iter_pages("123", country="us", limit=2))

        assert pages[-1].stopped_because == "exhausted"

    def test_no_limit_walks_to_exhaustion(self):
        provider = FakeProvider(
            [_page([_review(NOW, "a")], "1"), _page([_review(NOW, "b")], None)]
        )
        pages = list(FakeClient(provider).iter_pages("123", country="us"))

        assert len(pages) == 2
        assert pages[-1].stopped_because == "exhausted"

    async def test_aiter_pages_honours_limit(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a"), _review(NOW, "b")], "1"),
                _page([_review(NOW, "c")], "2"),
            ]
        )
        pages = [
            p
            async for p in FakeClient(provider).aiter_pages(
                "123", country="us", limit=2
            )
        ]

        assert len(pages) == 1
        assert pages[-1].stopped_because == "limit"
        assert len(provider.calls) == 1


class TestErrorHandling:
    def test_yields_the_error_page_then_stops(self):
        error = FetchError(country="us", message="boom", kind="server", status=503)
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                PageResult(error=error),
                _page([_review(NOW, "never")], None),
            ]
        )
        pages = list(FakeClient(provider).iter_pages("123", country="us"))

        assert len(pages) == 2
        assert pages[-1].error is error
        assert pages[-1].stopped_because == "error"
        assert len(provider.calls) == 2

    def test_does_not_raise(self):
        provider = FakeProvider(
            [PageResult(error=FetchError(country="us", message="x", kind="auth"))]
        )
        pages = list(FakeClient(provider).iter_pages("123", country="us"))

        assert pages[0].error is not None

    def test_error_on_the_first_page_still_yields_it(self):
        provider = FakeProvider(
            [PageResult(error=FetchError(country="us", message="x", kind="auth"))]
        )
        pages = list(FakeClient(provider).iter_pages("123", country="us"))

        assert len(pages) == 1
        assert pages[0].stopped_because == "error"


class TestAsyncParity:
    async def test_aiter_pages_matches_iter_pages(self):
        def build():
            return FakeProvider(
                [
                    _page([_review(NOW, "a")], "1"),
                    _page([_review(NOW, "b")], None),
                ]
            )

        sync_pages = list(FakeClient(build()).iter_pages("123", country="us"))
        async_provider = build()
        async_pages = [
            p async for p in FakeClient(async_provider).aiter_pages("123", country="us")
        ]

        assert [r.id for p in sync_pages for r in p.reviews] == [
            r.id for p in async_pages for r in p.reviews
        ]
        assert [p.stopped_because for p in sync_pages] == [
            p.stopped_because for p in async_pages
        ]
        # Proves aiter_pages actually drove afetch_page, not fetch_page.
        assert async_provider.async_calls == async_provider.calls
        assert len(async_provider.async_calls) == len(async_pages)

    async def test_aiter_pages_honours_the_since_early_stop(self):
        old = NOW - timedelta(days=30)
        provider = FakeProvider(
            [
                _page([_review(NOW, "new")], "1"),
                _page([_review(old, "old")], "2"),
                _page([_review(old, "older")], "3"),
            ]
        )
        pages = [
            p
            async for p in FakeClient(provider).aiter_pages(
                "123", country="us", since=NOW - timedelta(days=2)
            )
        ]

        assert len(pages) == 2
        assert len(provider.calls) == 2

    async def test_afetch_page_returns_one_page(self):
        provider = FakeProvider([_page([_review(NOW)], "1")])

        page = await FakeClient(provider).afetch_page("123", country="us")

        assert page.next_cursor == "1"
        assert len(provider.calls) == 1


class TestResolveCountries:
    def test_defaults_to_us_for_per_country_providers(self):
        assert FakeClient(FakeProvider([])).resolve_countries() == ["us"]

    def test_passes_requested_through(self):
        client = FakeClient(FakeProvider([]))
        assert client.resolve_countries(["gb", "de"]) == ["gb", "de"]

    def test_collapses_to_the_global_sentinel(self):
        client = FakeClient(FakeProvider([], source="appstore_official"))
        assert client.resolve_countries(["gb", "de"]) == [""]


class TestPublicRequestBudget:
    def test_iter_pages_honours_max_pages(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                _page([_review(NOW, "b")], "2"),
                _page([_review(NOW, "c")], None),
            ]
        )

        pages = list(FakeClient(provider).iter_pages("123", max_pages=2))

        assert len(pages) == 2
        assert pages[-1].stopped_because == "max_pages"
        assert len(provider.calls) == 2

    async def test_aiter_pages_honours_max_pages(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                _page([_review(NOW, "b")], "2"),
                _page([_review(NOW, "c")], None),
            ]
        )

        pages = [
            page async for page in FakeClient(provider).aiter_pages("123", max_pages=2)
        ]

        assert len(pages) == 2
        assert pages[-1].stopped_because == "max_pages"
        assert len(provider.calls) == 2

    def test_zero_limit_iter_pages_does_no_io(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        assert list(FakeClient(provider).iter_pages("123", limit=0)) == []
        assert provider.calls == []

    def test_negative_limit_iter_pages_is_rejected(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        with pytest.raises(ValueError, match="limit"):
            list(FakeClient(provider).iter_pages("123", limit=-1))
        assert provider.calls == []


class _CyclingProvider:
    """Hands back the same cursor forever, as a stalled API would.

    Bounded at ``_GIVE_UP`` calls so an unterminated walk fails the test instead
    of hanging it; the bug this guards against is an endless loop.
    """

    source = "appstore_scraper"
    _GIVE_UP = 50

    def __init__(self, cursor: str = "SAME"):
        self._cursor = cursor
        self.calls = 0

    def fetch_page(self, app_id, country, cursor):
        self.calls += 1
        if self.calls > self._GIVE_UP:
            raise AssertionError(
                f"the walk did not stop after {self._GIVE_UP} identical cursors"
            )
        return _page([_review(NOW, review_id=f"r{self.calls}")], self._cursor)

    async def afetch_page(self, app_id, country, cursor):
        return self.fetch_page(app_id, country, cursor)


class TestSinceIsAppliedToStreamedReviews:
    """``since`` bounds the walk *and* the reviews.

    Stopping the walk on the boundary page still yields that page, so the
    reviews on it that predate ``since`` reached the caller, up to a full page
    of them, which is 200 on Play. ``fetch`` filtered them out afterwards;
    ``iter_reviews`` had nothing to.
    """

    def _client(self):
        inside = [_review(NOW, "r0"), _review(NOW - timedelta(days=1), "r1")]
        straddling = [
            _review(NOW - timedelta(days=2), "r2"),
            _review(NOW - timedelta(days=90), "old-1"),
            _review(NOW - timedelta(days=120), "old-2"),
        ]
        return FakeClient(FakeProvider([_page(inside, "1"), _page(straddling, "2")]))

    def test_nothing_older_than_since_is_yielded(self):
        since = NOW - timedelta(days=10)
        streamed = list(self._client().iter_reviews("123", since=since))

        assert [r.id for r in streamed] == ["r0", "r1", "r2"]
        assert all(r.dated_at >= since for r in streamed)

    async def test_the_async_stream_agrees(self):
        since = NOW - timedelta(days=10)
        streamed = [r async for r in self._client().aiter_reviews("123", since=since)]

        assert [r.id for r in streamed] == ["r0", "r1", "r2"]

    def test_limit_counts_what_was_yielded_not_what_was_walked(self):
        """A review dropped by ``since`` must not consume the caller's limit."""
        since = NOW - timedelta(days=10)
        streamed = list(self._client().iter_reviews("123", since=since, limit=3))

        assert [r.id for r in streamed] == ["r0", "r1", "r2"]

    def test_fetch_and_iter_reviews_agree(self):
        since = NOW - timedelta(days=10)
        fetched = [r.id for r in self._client().fetch("123", since=since).reviews]
        streamed = [r.id for r in self._client().iter_reviews("123", since=since)]

        assert streamed == fetched


class TestExhaustedOutranksSince:
    """The rule ``StopPolicy`` states for ``limit`` has to hold for ``since`` too:
    "there is no more data" is never reported as "we stopped asking"."""

    def test_a_final_page_that_also_predates_since_reports_exhausted(self):
        page = _page([_review(NOW - timedelta(days=90), "old")], None)
        client = FakeClient(FakeProvider([page]))

        pages = list(client.iter_pages("123", since=NOW - timedelta(days=10)))

        assert pages[-1].stopped_because == "exhausted"

    def test_a_mid_walk_page_that_predates_since_still_reports_since(self):
        pages = [
            _page([_review(NOW - timedelta(days=90), "old")], "1"),
            _page([_review(NOW, "never-reached")], None),
        ]
        provider = FakeProvider(pages)
        client = FakeClient(provider)

        walked = list(client.iter_pages("123", since=NOW - timedelta(days=10)))

        assert walked[-1].stopped_because == "since"
        assert len(provider.calls) == 1


class TestARepeatingCursorEndsTheWalk:
    """A cursor that stops advancing is remote input driving an endless loop.

    Nothing capped it: the App Store RSS feed has its own page ceiling, but
    Connect and both Play sources rely on the endpoint to stop handing out
    cursors.
    """

    def test_the_walk_stops_instead_of_spinning(self):
        provider = _CyclingProvider()
        client = FakeClient(provider)

        pages = list(client.iter_pages("123"))

        assert provider.calls == 2
        assert pages[-1].stopped_because == "cycle"

    def test_a_cycle_is_not_reported_as_exhausted(self):
        """``exhausted`` claims there is no more data, which is not what happened."""
        client = FakeClient(_CyclingProvider())

        pages = list(client.iter_pages("123"))

        assert pages[-1].stopped_because != "exhausted"

    async def test_the_async_walk_stops_too(self):
        provider = _CyclingProvider()
        client = FakeClient(provider)

        pages = [p async for p in client.aiter_pages("123")]

        assert provider.calls == 2
        assert pages[-1].stopped_because == "cycle"

    def test_iter_reviews_does_not_spin_either(self):
        provider = _CyclingProvider()

        streamed = list(FakeClient(provider).iter_reviews("123"))

        assert len(streamed) == 2


class _FreshCursorProvider:
    """Hands back a *new* cursor every call, as an endpoint with no end would.

    A repeated cursor is caught by ``cycle``; a fresh one defeats it, which is
    what makes this the harder case. ``reviews_per_page=0`` additionally defeats
    ``limit`` and ``since``, since both are driven by reviews actually seen.

    Bounded at ``_GIVE_UP`` so an unterminated walk fails rather than hangs.
    """

    source = "appstore_scraper"
    _GIVE_UP = 500

    def __init__(self, reviews_per_page: int = 1):
        self._per_page = reviews_per_page
        self.calls = 0

    def fetch_page(self, app_id, country, cursor):
        self.calls += 1
        if self.calls > self._GIVE_UP:
            raise AssertionError(
                f"the walk did not stop after {self._GIVE_UP} fresh cursors"
            )
        reviews = [
            _review(NOW, review_id=f"r{self.calls}-{i}") for i in range(self._per_page)
        ]
        return _page(reviews, f"cursor-{self.calls}")

    async def afetch_page(self, app_id, country, cursor):
        return self.fetch_page(app_id, country, cursor)


class TestASourceThatNeverStopsEndsTheWalk:
    """A fresh cursor every page defeats ``cycle``; empty pages defeat the rest.

    ``limit`` counts reviews seen and ``since`` reads a page's oldest review, so
    a source that returns no reviews but keeps issuing cursors escapes every
    caller-supplied bound at once. The walk needs its own floor.
    """

    def test_empty_pages_with_fresh_cursors_stop_as_stalled(self):
        provider = _FreshCursorProvider(reviews_per_page=0)

        pages = list(FakeClient(provider).iter_pages("123"))

        assert pages[-1].stopped_because == "stalled"
        assert provider.calls == DEFAULT_MAX_EMPTY_PAGES

    def test_a_limit_does_not_rescue_an_empty_walk_but_stalled_does(self):
        """``limit=5`` reads as bounded to a caller. Before, it was not."""
        provider = _FreshCursorProvider(reviews_per_page=0)

        pages = list(FakeClient(provider).iter_pages("123", limit=5))

        assert pages[-1].stopped_because == "stalled"

    def test_since_does_not_rescue_an_empty_walk_either(self):
        provider = _FreshCursorProvider(reviews_per_page=0)

        pages = list(FakeClient(provider).iter_pages("123", since=NOW))

        assert pages[-1].stopped_because == "stalled"

    async def test_the_async_walk_stalls_too(self):
        provider = _FreshCursorProvider(reviews_per_page=0)

        pages = [p async for p in FakeClient(provider).aiter_pages("123")]

        assert pages[-1].stopped_because == "stalled"

    def test_iter_reviews_and_fetch_terminate_as_well(self):
        assert list(FakeClient(_FreshCursorProvider(0)).iter_reviews("123")) == []
        result = FakeClient(_FreshCursorProvider(0)).fetch("123", concurrency=1)
        assert result.outcomes[-1].stopped_because == "stalled"

    def test_one_empty_page_mid_walk_does_not_end_it(self):
        """A source may return an empty page and then more data. Only a *streak*
        means it has stopped advancing."""
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                _page([], "2"),
                _page([_review(NOW, "b")], None),
            ]
        )

        pages = list(FakeClient(provider).iter_pages("123"))

        assert [r.id for page in pages for r in page.reviews] == ["a", "b"]
        assert pages[-1].stopped_because == "exhausted"

    def test_a_non_empty_page_resets_the_streak(self):
        """Only a *consecutive* run counts, so intermittent empty pages survive."""
        policy = StopPolicy("appstore_scraper", None, None, max_empty_pages=3)

        assert policy.evaluate(_page([], "c1"))[0] is None
        assert policy.evaluate(_page([], "c2"))[0] is None
        assert policy.evaluate(_page([_review(NOW)], "c3"))[0] is None  # streak reset
        assert policy.evaluate(_page([], "c4"))[0] is None
        assert policy.evaluate(_page([], "c5"))[0] is None
        assert policy.evaluate(_page([], "c6"))[0] == "stalled"


class TestThePageCeiling:
    """A source returning data forever is still bounded, and so is the memory
    the walk spends detecting cycles."""

    def test_a_never_exhausting_source_stops_at_the_ceiling(self):
        policy = StopPolicy("appstore_scraper", None, None, max_pages=5)
        reasons = [policy.evaluate(_page([_review(NOW)], f"c{i}"))[0] for i in range(5)]

        assert reasons[:-1] == [None, None, None, None]
        assert reasons[-1] == "max_pages"

    def test_the_ceiling_bounds_retained_cursors(self):
        policy = StopPolicy("appstore_scraper", None, None, max_pages=5)
        for i in range(5):
            policy.evaluate(_page([_review(NOW)], f"c{i}"))

        assert len(policy._cursors) <= 5

    def test_the_ceiling_is_not_reported_as_exhausted(self):
        """More data exists; ``exhausted`` would claim it does not."""
        policy = StopPolicy("appstore_scraper", None, None, max_pages=1)

        assert policy.evaluate(_page([_review(NOW)], "c0"))[0] == "max_pages"

    def test_a_walk_that_ends_on_its_own_is_unaffected(self):
        policy = StopPolicy("appstore_scraper", None, None, max_pages=5)

        assert policy.evaluate(_page([_review(NOW)], None))[0] == "exhausted"


class _RaisingProvider:
    """Fails by raising rather than by returning an error.

    ``GoogleAuth.authorization_header`` performs a token exchange, so it can fail
    with ``TransportError``/``ServerError``/``ParseError`` (raised, not returned)
    and providers call it outside any try.
    """

    source = "appstore_scraper"

    def __init__(self, fail_after: int = 1, exc: Exception | None = None):
        self._fail_after = fail_after
        self._exc = exc or TransportError("token endpoint unreachable", status=0)
        self.calls = 0

    def fetch_page(self, app_id, country, cursor):
        self.calls += 1
        if self.calls > self._fail_after:
            raise self._exc
        return _page([_review(NOW, f"r{self.calls}")], f"{self.calls}")

    async def afetch_page(self, app_id, country, cursor):
        return self.fetch_page(app_id, country, cursor)


class _PerCountryRaiser:
    """Walks every country but one, which raises."""

    source = "appstore_scraper"

    def __init__(self, failing: str):
        self._failing = failing

    def fetch_page(self, app_id, country, cursor):
        if country == self._failing:
            raise ServerError("token endpoint returned 503", status=503)
        return _page([_review(NOW, country)], None)

    async def afetch_page(self, app_id, country, cursor):
        return self.fetch_page(app_id, country, cursor)


class TestARaisedErrorBecomesPageData:
    """``iter_pages`` documents that it never raises. That has to hold for a
    provider that raises as well as one that returns an error."""

    def test_iter_pages_reports_instead_of_raising(self):
        pages = list(FakeClient(_RaisingProvider()).iter_pages("123"))

        assert pages[-1].error is not None
        assert pages[-1].error.kind == "transport"
        assert pages[-1].stopped_because == "error"

    def test_the_pages_walked_before_the_failure_survive(self):
        pages = list(FakeClient(_RaisingProvider(fail_after=2)).iter_pages("123"))

        assert [r.id for p in pages for r in p.reviews] == ["r1", "r2"]

    def test_the_message_is_preserved(self):
        pages = list(FakeClient(_RaisingProvider()).iter_pages("123"))

        assert "token endpoint unreachable" in pages[-1].error.message

    async def test_the_async_walk_reports_it_too(self):
        pages = [p async for p in FakeClient(_RaisingProvider()).aiter_pages("123")]

        assert pages[-1].error is not None
        assert pages[-1].error.kind == "transport"

    def test_a_programming_error_still_propagates(self):
        """A stdlib exception is a bug in here, not a failure out there, and must
        not be dressed up as a ``FetchError``."""
        provider = _RaisingProvider(exc=KeyError("attributes"))

        with pytest.raises(KeyError):
            list(FakeClient(provider).iter_pages("123"))


class TestOneFailingCountryKeepsTheRest:
    """``fetch`` collected results with ``[f.result() for f in futures]``, so the
    first country that raised discarded every country that had already walked."""

    def test_the_other_countries_reviews_survive(self):
        client = FakeClient(_PerCountryRaiser("gb"))

        result = client.fetch("123", countries=["us", "gb", "de"], concurrency=1)

        assert sorted(r.id for r in result.reviews) == ["de", "us"]

    def test_the_failure_is_reported_rather_than_raised(self):
        client = FakeClient(_PerCountryRaiser("gb"))

        result = client.fetch("123", countries=["us", "gb", "de"], concurrency=1)

        assert [e.country for e in result.errors] == ["gb"]
        assert result.errors[0].kind == "server"

    def test_it_holds_under_a_concurrent_fan_out(self):
        client = FakeClient(_PerCountryRaiser("gb"))

        result = client.fetch("123", countries=["us", "gb", "de"])

        assert sorted(r.id for r in result.reviews) == ["de", "us"]
        assert len(result.errors) == 1

    async def test_the_async_fan_out_agrees(self):
        client = FakeClient(_PerCountryRaiser("gb"))

        result = await client.afetch("123", countries=["us", "gb", "de"])

        assert sorted(r.id for r in result.reviews) == ["de", "us"]
        assert len(result.errors) == 1


class TestCountriesOnAGlobalSourceSaysSo:
    """A global source collapses any country list to one request. Doing that
    silently let a caller believe they had fetched a German slice."""

    def _global_client(self):
        return FakeClient(
            FakeProvider(
                [_page([_review(NOW, "r1")], None)], source="appstore_official"
            )
        )

    def test_it_warns_that_the_request_was_ignored(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            self._global_client().fetch("123", countries=["de", "fr"])

        assert "appstore_official" in caplog.text
        assert "de" in caplog.text

    def test_asking_for_nothing_does_not_warn(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            self._global_client().fetch("123")

        assert caplog.text == ""

    def test_a_per_country_source_does_not_warn(self, caplog):
        import logging

        client = FakeClient(FakeProvider([_page([_review(NOW, "r1")], None)]))
        with caplog.at_level(logging.WARNING):
            client.fetch("123", countries=["de"])

        assert caplog.text == ""


class TestAnUnusableCredentialStillRaises:
    """``AuthError`` is configuration, not a per-page failure.

    Every country and every page would repeat it, so a ``FetchResult`` carrying N
    identical auth errors and no reviews is worse than one exception the caller
    can act on. The typed hierarchy exists so this is catchable; see
    ``tests/app_reviews/appstore/test_reviews.py``.
    """

    def test_iter_pages_raises_it(self):
        provider = _RaisingProvider(exc=AuthError("key rejected", status=401))

        with pytest.raises(AuthError):
            list(FakeClient(provider).iter_pages("123"))

    def test_fetch_raises_it(self):
        provider = _RaisingProvider(exc=AuthError("key rejected", status=401))

        with pytest.raises(AuthError):
            FakeClient(provider).fetch("123")

    async def test_the_async_ladder_raises_it_too(self):
        provider = _RaisingProvider(exc=AuthError("key rejected", status=401))

        with pytest.raises(AuthError):
            [p async for p in FakeClient(provider).aiter_pages("123")]


class TestSortAcceptsAnEquivalentString:
    """``Sort`` is a ``StrEnum`` and ``FetchResult.sort`` coerces, so ``fetch``
    must too: ``_walk_limit`` compared with ``is`` and then read ``.value``."""

    @pytest.mark.parametrize("order", ["newest", "oldest", "rating"])
    def test_a_plain_string_with_a_limit_does_not_crash(self, order):
        client = FakeClient(FakeProvider([_page([_review(NOW, "1")], None)]))

        assert len(client.fetch("123", sort=order, limit=5).reviews) == 1

    def test_the_string_newest_still_bounds_the_walk(self):
        """``is not Sort.NEWEST`` was true for the string, so the early stop was
        silently disabled and an INFO line claimed newest-first could not bound it."""
        pages = [
            _page([_review(NOW, str(i)) for i in range(5)], str(i + 1))
            for i in range(4)
        ]
        provider = FakeProvider(pages)

        FakeClient(provider).fetch("123", sort="newest", limit=5)

        assert len(provider.calls) == 1

    @pytest.mark.parametrize("order", ["newest", Sort.NEWEST])
    def test_string_and_member_agree(self, order):
        client = FakeClient(FakeProvider([_page([_review(NOW, "1")], None)]))

        assert len(client.fetch("123", sort=order, limit=5).reviews) == 1


class TestTheStreamLimitCountsYieldedReviews:
    """``limit`` on ``iter_reviews`` means "yield at most this many".

    It was handed to ``StopPolicy`` as the *walk* limit, which counts reviews
    fetched, so on a source with no newest-first guarantee, reviews dropped by
    the stream's own ``since`` filter consumed the caller's budget and the walk
    stopped with matching reviews unfetched on the next page.
    """

    def _unordered(self):
        # googleplay_official: ordered_newest_first is False, so the `since`
        # early stop never fires and every page is walked.
        old, new = NOW - timedelta(days=90), NOW
        return FakeProvider(
            [
                _page(
                    [_review(old, f"o{i}") for i in range(4)] + [_review(new, "n0")],
                    "1",
                ),
                _page([_review(new, f"n{i}") for i in range(1, 6)], None),
            ],
            source="googleplay_official",
        )

    def test_it_yields_the_number_asked_for(self):
        got = list(
            FakeClient(self._unordered()).iter_reviews(
                "123", since=NOW - timedelta(days=10), limit=5
            )
        )

        assert [r.id for r in got] == ["n0", "n1", "n2", "n3", "n4"]

    async def test_the_async_stream_agrees(self):
        got = [
            r
            async for r in FakeClient(self._unordered()).aiter_reviews(
                "123", since=NOW - timedelta(days=10), limit=5
            )
        ]

        assert len(got) == 5

    def test_it_matches_fetch_for_the_same_arguments(self):
        since = NOW - timedelta(days=10)
        streamed = [
            r.id
            for r in FakeClient(self._unordered()).iter_reviews(
                "123", since=since, limit=5
            )
        ]
        fetched = [
            r.id
            for r in FakeClient(self._unordered())
            .fetch("123", since=since, limit=5)
            .reviews
        ]

        assert sorted(streamed) == sorted(fetched)


class TestZeroAndNegativeLimits:
    """``limit=0`` means none. The check ran after the yield, so one escaped."""

    def _client(self):
        return FakeClient(
            FakeProvider([_page([_review(NOW, "1"), _review(NOW, "2")], None)])
        )

    def test_iter_reviews_yields_nothing_for_zero(self):
        assert list(self._client().iter_reviews("123", limit=0)) == []

    async def test_aiter_reviews_yields_nothing_for_zero(self):
        assert [r async for r in self._client().aiter_reviews("123", limit=0)] == []

    def test_fetch_returns_nothing_for_zero(self):
        assert self._client().fetch("123", limit=0).reviews == []

    @pytest.mark.parametrize("limit", [-1, -5])
    def test_sync_entry_points_reject_negative_limits(self, limit):
        with pytest.raises(ValueError, match="limit"):
            list(self._client().iter_reviews("123", limit=limit))
        with pytest.raises(ValueError, match="limit"):
            self._client().fetch("123", limit=limit)

    @pytest.mark.parametrize("limit", [-1, -5])
    async def test_async_entry_points_reject_negative_limits(self, limit):
        with pytest.raises(ValueError, match="limit"):
            [r async for r in self._client().aiter_reviews("123", limit=limit)]
        with pytest.raises(ValueError, match="limit"):
            await self._client().afetch("123", limit=limit)


class TestAsyncFanOutDoesNotAbandonItsPeers:
    """``asyncio.gather`` cancels its siblings on the first exception, so a
    country that raised left the others' in-flight requests orphaned on the
    shared pool. The threaded sync path waits for all of them."""

    class _SlowExceptFor:
        source = "appstore_scraper"

        def __init__(self, failing):
            self.failing = failing
            self.finished: list[str] = []

        def fetch_page(self, app_id, country, cursor):
            if country == self.failing:
                raise AuthError("bad key", status=401)
            self.finished.append(country)
            return _page([_review(NOW, country)], None)

        async def afetch_page(self, app_id, country, cursor):
            if country == self.failing:
                raise AuthError("bad key", status=401)
            await asyncio.sleep(0.05)
            self.finished.append(country)
            return _page([_review(NOW, country)], None)

    async def test_the_other_countries_finish_before_the_error_surfaces(self):
        provider = self._SlowExceptFor("us")
        with pytest.raises(AuthError):
            await FakeClient(provider).afetch("123", countries=["us", "gb", "de"])

        assert sorted(provider.finished) == ["de", "gb"]


class TestSinceAndUntilAreInclusiveAtTheBoundary:
    """A review exactly *at* the boundary is kept on both sides.

    Flipping ``>=`` to ``>`` or ``<=`` to ``<`` in ``FetchResult.filter`` passed
    the whole suite, because no test had a review on the boundary. ``_predates_since``
    uses ``<``, which only pairs with ``>=``; if one flips they disagree and the
    walk stops on a page it should have kept. An incremental ``since=last_run``
    sync drops or duplicates exactly one review per run.
    """

    def _result(self):
        boundary = NOW - timedelta(days=5)
        return FakeClient(
            FakeProvider(
                [
                    _page(
                        [
                            _review(NOW, "newer"),
                            _review(boundary, "on-the-boundary"),
                            _review(NOW - timedelta(days=10), "older"),
                        ],
                        None,
                    )
                ]
            )
        ), boundary

    def test_since_keeps_a_review_exactly_on_it(self):
        client, boundary = self._result()

        ids = [r.id for r in client.fetch("123", since=boundary).reviews]

        assert "on-the-boundary" in ids
        assert "older" not in ids

    def test_until_keeps_a_review_exactly_on_it(self):
        client, boundary = self._result()

        ids = [r.id for r in client.fetch("123", until=boundary).reviews]

        assert "on-the-boundary" in ids
        assert "newer" not in ids

    def test_the_stream_agrees_with_fetch_on_the_boundary(self):
        client, boundary = self._result()
        streamed = [r.id for r in client.iter_reviews("123", since=boundary)]

        assert "on-the-boundary" in streamed
        assert "older" not in streamed

    def test_a_naive_datetime_is_read_as_utc(self):
        """``datetime.now()`` is the likeliest thing a caller passes, and an
        aware/naive comparison raises ``TypeError``."""
        client, boundary = self._result()

        ids = [
            r.id
            for r in client.fetch("123", since=boundary.replace(tzinfo=None)).reviews
        ]

        assert "on-the-boundary" in ids

    def test_a_plain_date_is_accepted(self):
        client, boundary = self._result()

        assert client.fetch("123", since=boundary.date()) is not None


class TestResumingFromAPersistedCursor:
    """The ladder's headline feature. Three mutations passed the suite:
    ``with_stop_reason`` dropping ``next_cursor``, and both async entry points
    ignoring the ``cursor`` they were handed."""

    def _pages(self):
        return [
            _page([_review(NOW, "p1")], "1"),
            _page([_review(NOW, "p2")], "2"),
            _page([_review(NOW, "p3")], None),
        ]

    def test_a_yielded_page_keeps_its_cursor(self):
        """``with_stop_reason`` rebuilds the page; dropping ``next_cursor`` there
        makes every walk one page long."""
        provider = FakeProvider(self._pages())
        pages = list(FakeClient(provider).iter_pages("123", limit=1))

        assert pages[-1].next_cursor == "1"
        assert pages[-1].stopped_because == "limit"

    def test_a_yielded_page_keeps_its_skipped_review_count(self):
        provider = FakeProvider(
            [
                PageResult(
                    reviews=[_review(NOW, "p1")],
                    next_cursor="1",
                    skipped_reviews=2,
                )
            ]
        )

        [page] = list(FakeClient(provider).iter_pages("123", limit=1))

        assert page.stopped_because == "limit"
        assert page.skipped_reviews == 2

    def test_a_walk_resumes_where_the_cursor_left_off(self):
        provider = FakeProvider(self._pages())
        client = FakeClient(provider)

        first = client.fetch_page("123", country="us")
        resumed = client.fetch_page("123", country="us", cursor=first.next_cursor)

        assert [r.id for r in first.reviews] == ["p1"]
        assert [r.id for r in resumed.reviews] == ["p2"]
        assert provider.calls == [("us", None), ("us", "1")]

    async def test_the_async_page_honours_its_cursor(self):
        provider = FakeProvider(self._pages())
        page = await FakeClient(provider).afetch_page("123", country="us", cursor="1")

        assert [r.id for r in page.reviews] == ["p2"]
        assert provider.async_calls == [("us", "1")]

    async def test_the_async_walk_starts_from_its_cursor(self):
        provider = FakeProvider(self._pages())
        pages = [p async for p in FakeClient(provider).aiter_pages("123", cursor="1")]

        assert [r.id for p in pages for r in p.reviews] == ["p2", "p3"]
        assert provider.async_calls[0] == ("us", "1")

    def test_the_sync_walk_starts_from_its_cursor(self):
        provider = FakeProvider(self._pages())
        pages = list(FakeClient(provider).iter_pages("123", cursor="1"))

        assert [r.id for p in pages for r in p.reviews] == ["p2", "p3"]


class TestPerCountry:
    def test_only_the_rss_feed_has_a_country_dimension(self):
        """Its URL is per storefront. Connect and both Play endpoints are global,
        so fanning out would repeat one request for identical data."""
        assert is_per_country("appstore_scraper") is True

    @pytest.mark.parametrize(
        "source", ["appstore_official", "googleplay_scraper", "googleplay_official"]
    )
    def test_the_global_sources_have_none(self, source):
        assert is_per_country(source) is False


class TestNewestFirst:
    @pytest.mark.parametrize(
        "source", ["appstore_scraper", "appstore_official", "googleplay_scraper"]
    )
    def test_the_ordered_sources(self, source):
        assert orders_newest_first(source) is True

    def test_the_developer_api_makes_no_ordering_promise(self):
        """It documents none, and stopping a walk early on an unverified
        assumption silently loses reviews."""
        assert orders_newest_first("googleplay_official") is False


class TestAnUnknownSourceDegradesSafely:
    """The old ``capabilities()`` raised ``KeyError`` for a source outside the
    literal, which made a custom provider crash the walk rather than work."""

    @pytest.mark.parametrize("predicate", [is_per_country, orders_newest_first])
    def test_it_answers_false_rather_than_raising(self, predicate):
        assert predicate("something_custom") is False


class TestTheTablesOnlyNameRealSources:
    @pytest.mark.parametrize("table", [_PER_COUNTRY, _NEWEST_FIRST])
    def test_every_entry_is_a_source_literal(self, table):
        assert table <= set(get_args(Source))
