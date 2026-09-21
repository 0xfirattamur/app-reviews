"""Tests for the full fetch: rung 3 of the client ladder."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Lock

import pytest

from app_reviews.models.country import Country
from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError
from app_reviews.models.types import Sort

from .test_paging import NOW, FakeClient, FakeProvider, _page, _review


class MultiCountryProvider:
    """Serves a different scripted page list per country.

    ``calls`` records every request regardless of entry point. ``async_calls``
    records only requests that went through ``afetch_page``, so a test can
    prove the async path actually used it instead of the sync ``fetch_page``
    body.
    """

    def __init__(self, pages_by_country, source="appstore_scraper"):
        self._pages = pages_by_country
        self.source = source
        self.calls: list[tuple[str, str | None]] = []
        self.async_calls: list[tuple[str, str | None]] = []

    def fetch_page(self, app_id, country, cursor):
        self.calls.append((country, cursor))
        return self._page_for(country, cursor)

    async def afetch_page(self, app_id, country, cursor):
        self.calls.append((country, cursor))
        self.async_calls.append((country, cursor))
        return self._page_for(country, cursor)

    def _page_for(self, country, cursor):
        pages = self._pages.get(country, [])
        index = 0 if cursor is None else int(cursor)
        if index >= len(pages):
            return PageResult()
        return pages[index]


class BlockingInFlightProvider:
    """Hold sync requests open so a public fetch's real fan-out is observable."""

    source = "appstore_scraper"

    def __init__(self, expected: int):
        self.expected = expected
        self.active = 0
        self.maximum = 0
        self.lock = Lock()
        self.reached = Event()
        self.exceeded = Event()
        self.release = Event()

    def fetch_page(self, app_id, country, cursor):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active >= self.expected:
                self.reached.set()
            if self.active > self.expected:
                self.exceeded.set()
        if not self.release.wait(timeout=2):
            raise AssertionError("fetch did not release blocked provider calls")
        with self.lock:
            self.active -= 1
        return PageResult()

    async def afetch_page(self, app_id, country, cursor):
        raise AssertionError("sync concurrency test used the async provider path")


class AsyncBlockingInFlightProvider:
    """Async twin of BlockingInFlightProvider."""

    source = "appstore_scraper"

    def __init__(self, expected: int):
        self.expected = expected
        self.active = 0
        self.maximum = 0
        self.reached = asyncio.Event()
        self.exceeded = asyncio.Event()
        self.release = asyncio.Event()

    def fetch_page(self, app_id, country, cursor):
        raise AssertionError("async concurrency test used the sync provider path")

    async def afetch_page(self, app_id, country, cursor):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        if self.active >= self.expected:
            self.reached.set()
        if self.active > self.expected:
            self.exceeded.set()
        try:
            await asyncio.wait_for(self.release.wait(), timeout=2)
        finally:
            self.active -= 1
        return PageResult()


class TestSingleCountry:
    def test_collects_reviews_across_pages(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                _page([_review(NOW, "b")], None),
            ]
        )
        result = FakeClient(provider).fetch("123", countries=["us"])

        assert [r.id for r in result.reviews] == ["a", "b"]

    def test_reports_one_outcome_per_country(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        result = FakeClient(provider).fetch("123", countries=["us"])

        assert len(result.outcomes) == 1
        assert result.outcomes[0].country == "us"
        assert result.outcomes[0].pages == 1
        assert result.outcomes[0].reviews_fetched == 1
        assert result.outcomes[0].stopped_because == "exhausted"

    def test_outcome_records_page_count(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                _page([_review(NOW, "b")], "2"),
                _page([_review(NOW, "c")], None),
            ]
        )
        result = FakeClient(provider).fetch("123", countries=["us"])

        assert result.outcomes[0].pages == 3

    def test_actual_walk_aggregates_skipped_reviews_into_the_result_envelope(self):
        provider = FakeProvider(
            [
                PageResult(
                    reviews=[_review(NOW, "a")],
                    next_cursor="1",
                    skipped_reviews=2,
                ),
                PageResult(
                    reviews=[_review(NOW, "b")],
                    skipped_reviews=1,
                ),
            ]
        )

        result = FakeClient(provider).fetch("123", countries=["us"])

        assert result.skipped_reviews == 3
        assert result.outcomes[0].skipped_reviews == 3
        assert result.to_dict()["skipped_reviews"] == 3
        assert result.to_dict()["outcomes"][0]["skipped_reviews"] == 3


class TestStopReasons:
    def test_limit_is_reported_as_the_stop_reason(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a"), _review(NOW, "b")], "1"),
                _page([_review(NOW, "c")], None),
            ]
        )
        result = FakeClient(provider).fetch("123", countries=["us"], limit=2)

        assert result.outcomes[0].stopped_because == "limit"
        assert len(result.reviews) == 2

    def test_exhausted_when_the_provider_runs_out(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        result = FakeClient(provider).fetch("123", countries=["us"], limit=100)

        assert result.outcomes[0].stopped_because == "exhausted"

    def test_since_is_reported_as_the_stop_reason(self):
        old = NOW - timedelta(days=30)
        provider = FakeProvider(
            [_page([_review(NOW, "new")], "1"), _page([_review(old, "old")], "2")]
        )
        result = FakeClient(provider).fetch(
            "123", countries=["us"], since=NOW - timedelta(days=2)
        )

        assert result.outcomes[0].stopped_because == "since"

    def test_error_is_reported_in_the_outcome_and_errors_list(self):
        error = FetchError(country="us", message="boom", kind="server", status=503)
        provider = FakeProvider([PageResult(error=error)])

        result = FakeClient(provider).fetch("123", countries=["us"])

        assert result.outcomes[0].stopped_because == "error"
        assert result.outcomes[0].error is error
        assert result.errors == [error]

    def test_partial_success_keeps_reviews_and_reports_the_error(self):
        error = FetchError(country="us", message="boom", kind="server", status=503)
        provider = FakeProvider(
            [_page([_review(NOW, "a")], "1"), PageResult(error=error)]
        )

        result = FakeClient(provider).fetch("123", countries=["us"])

        assert [r.id for r in result.reviews] == ["a"]
        assert result.errors == [error]
        assert result.outcomes[0].stopped_because == "error"


class TestMultipleCountries:
    def test_merges_reviews_from_every_country(self):
        provider = MultiCountryProvider(
            {
                "us": [_page([_review(NOW, "us1")], None)],
                "gb": [_page([_review(NOW, "gb1")], None)],
            }
        )
        result = FakeClient(provider).fetch("123", countries=["us", "gb"])

        assert {r.id for r in result.reviews} == {"us1", "gb1"}

    def test_one_outcome_per_country(self):
        provider = MultiCountryProvider(
            {
                "us": [_page([_review(NOW, "us1")], None)],
                "gb": [_page([_review(NOW, "gb1")], None)],
            }
        )
        result = FakeClient(provider).fetch("123", countries=["us", "gb"])

        assert {o.country for o in result.outcomes} == {"us", "gb"}

    def test_one_country_failing_does_not_lose_the_others(self):
        provider = MultiCountryProvider(
            {
                "us": [_page([_review(NOW, "us1")], None)],
                "gb": [
                    PageResult(error=FetchError(country="gb", message="x", kind="auth"))
                ],
            }
        )
        result = FakeClient(provider).fetch("123", countries=["us", "gb"])

        assert [r.id for r in result.reviews] == ["us1"]
        assert len(result.errors) == 1
        assert len(result.outcomes) == 2

    def test_concurrency_one_is_sequential(self):
        provider = MultiCountryProvider(
            {
                "us": [_page([_review(NOW, "us1")], None)],
                "gb": [_page([_review(NOW, "gb1")], None)],
            }
        )
        result = FakeClient(provider).fetch(
            "123", countries=["us", "gb"], concurrency=1
        )

        assert len(result.reviews) == 2
        assert [c[0] for c in provider.calls] == ["us", "gb"]

    def test_global_provider_makes_one_call_regardless_of_countries(self):
        provider = FakeProvider(
            [_page([_review(NOW)], None)],
            source="googleplay_official",
        )

        result = FakeClient(provider).fetch("123", countries=["us", "gb", "de"])

        assert len(provider.calls) == 1
        assert len(result.outcomes) == 1
        assert result.outcomes[0].country is None


class TestSortAndLimit:
    """limit means 'the N best under sort', not 'sort the first N fetched'."""

    def test_newest_with_limit_stops_paging_early(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a"), _review(NOW, "b")], "1"),
                _page([_review(NOW, "c")], "2"),
            ]
        )
        FakeClient(provider).fetch("123", countries=["us"], sort=Sort.NEWEST, limit=2)

        assert len(provider.calls) == 1

    def test_rating_with_limit_exhausts_pagination_first(self):
        newest_low = _review(NOW, "newest_low", rating=1)
        oldest_high = _review(NOW - timedelta(days=5), "oldest_high", rating=5)

        provider = FakeProvider([_page([newest_low], "1"), _page([oldest_high], None)])
        result = FakeClient(provider).fetch(
            "123", countries=["us"], sort=Sort.RATING, limit=1
        )

        assert len(provider.calls) == 2
        assert [r.id for r in result.reviews] == ["oldest_high"]

    def test_oldest_with_limit_exhausts_pagination_first(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "newest")], "1"),
                _page([_review(NOW - timedelta(days=10), "oldest")], None),
            ]
        )
        result = FakeClient(provider).fetch(
            "123", countries=["us"], sort=Sort.OLDEST, limit=1
        )

        assert len(provider.calls) == 2
        assert [r.id for r in result.reviews] == ["oldest"]

    def test_newest_with_limit_cannot_early_stop_without_ordering(self):
        provider = FakeProvider(
            [
                _page([_review(NOW - timedelta(days=10), "old")], "1"),
                _page([_review(NOW, "new")], None),
            ],
            source="googleplay_official",
        )
        result = FakeClient(provider).fetch("123", sort=Sort.NEWEST, limit=1)

        assert len(provider.calls) == 2
        assert [r.id for r in result.reviews] == ["new"]


class TestFilters:
    def test_until_filters_the_returned_set(self):
        provider = FakeProvider(
            [
                _page(
                    [_review(NOW, "new"), _review(NOW - timedelta(days=10), "old")],
                    None,
                )
            ]
        )
        result = FakeClient(provider).fetch(
            "123", countries=["us"], until=NOW - timedelta(days=5)
        )

        assert [r.id for r in result.reviews] == ["old"]

    def test_since_filters_the_boundary_exactly(self):
        old = _review(NOW - timedelta(days=10), "old")
        provider = FakeProvider([_page([_review(NOW, "new"), old], None)])
        result = FakeClient(provider).fetch(
            "123", countries=["us"], since=NOW - timedelta(days=2)
        )

        assert [r.id for r in result.reviews] == ["new"]

    def test_ratings_filter(self):
        low = _review(NOW, "low", rating=1)
        provider = FakeProvider([_page([_review(NOW, "high"), low], None)])

        result = FakeClient(provider).fetch("123", countries=["us"], ratings=[1])

        assert [r.id for r in result.reviews] == ["low"]


class TestLimitWithFilters:
    """Filtered limits count qualifying reviews, never raw rows.

    A newest-first walk may stop after N matches. It must keep paging while the
    raw rows do not match, and unordered/other-sort walks remain exhaustive.
    """

    def test_until_with_limit_does_not_return_empty_when_matches_exist_later(self):
        # Page 1 alone already has `limit` (4) reviews, none of which
        # satisfy `until`. A walk that stops at 4 unfiltered reviews never
        # reaches page 2, where the matching reviews live.
        provider = FakeProvider(
            [
                _page(
                    [
                        _review(NOW, "new1"),
                        _review(NOW, "new2"),
                        _review(NOW, "new3"),
                        _review(NOW, "new4"),
                    ],
                    "1",
                ),
                _page(
                    [
                        _review(NOW - timedelta(days=12), "old1"),
                        _review(NOW - timedelta(days=13), "old2"),
                    ],
                    None,
                ),
            ]
        )
        result = FakeClient(provider).fetch(
            "123", countries=["us"], until=NOW - timedelta(days=10), limit=4
        )

        assert [r.id for r in result.reviews] == ["old1", "old2"]
        # The walk must exhaust pagination, not stop after page 1's 4
        # unfiltered reviews.
        assert len(provider.calls) == 2

    def test_ratings_with_limit_does_not_return_empty_when_matches_exist_later(self):
        provider = FakeProvider(
            [
                _page(
                    [_review(NOW, "r5_a", rating=5), _review(NOW, "r5_b", rating=5)],
                    "1",
                ),
                _page(
                    [_review(NOW, "r1_a", rating=1), _review(NOW, "r1_b", rating=1)],
                    None,
                ),
            ]
        )
        result = FakeClient(provider).fetch(
            "123", countries=["us"], ratings=[1], limit=2
        )

        assert {r.id for r in result.reviews} == {"r1_a", "r1_b"}
        assert len(provider.calls) == 2

    def test_until_with_limit_returns_the_n_most_recent_matches(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "too_new")], "1"),
                _page(
                    [
                        _review(NOW - timedelta(days=6), "old1"),
                        _review(NOW - timedelta(days=7), "old2"),
                        _review(NOW - timedelta(days=8), "old3"),
                    ],
                    None,
                ),
            ]
        )
        result = FakeClient(provider).fetch(
            "123", countries=["us"], until=NOW - timedelta(days=5), limit=2
        )

        assert [r.id for r in result.reviews] == ["old1", "old2"]

    def test_since_alone_still_bounds_the_walk(self):
        """`since` keeps its own early stop; only `ratings`/`until` force
        exhaustion."""
        provider = FakeProvider(
            [
                _page([_review(NOW, "a"), _review(NOW, "b")], "1"),
                _page([_review(NOW, "c")], "2"),
            ]
        )
        FakeClient(provider).fetch(
            "123",
            countries=["us"],
            since=NOW - timedelta(days=2),
            sort=Sort.NEWEST,
            limit=2,
        )

        assert len(provider.calls) == 1

    @pytest.mark.parametrize(
        ("kwargs", "expected_ids", "expected_calls"),
        [
            ({"ratings": [1]}, ["new-low"], 1),
            ({"until": NOW - timedelta(hours=12)}, ["new-high"], 1),
            (
                {"ratings": [5], "until": NOW - timedelta(hours=12)},
                ["new-high"],
                1,
            ),
            ({"ratings": [1], "sort": Sort.OLDEST}, ["old-low"], 2),
            ({"sort": Sort.RATING}, ["new-high"], 2),
        ],
    )
    def test_ordered_fetch_stops_only_when_the_requested_result_is_known(
        self, kwargs, expected_ids, expected_calls
    ):
        provider = FakeProvider(
            [
                _page(
                    [
                        _review(NOW, "new-low", rating=1),
                        _review(NOW - timedelta(days=1), "new-high", rating=5),
                    ],
                    "1",
                ),
                _page([_review(NOW - timedelta(days=2), "old-low", rating=1)], None),
            ]
        )

        result = FakeClient(provider).fetch("123", countries=["us"], limit=1, **kwargs)

        assert [review.id for review in result.reviews] == expected_ids
        assert len(provider.calls) == expected_calls
        if expected_calls == 1:
            assert result.outcomes[0].stopped_because == "limit"

    def test_unordered_source_remains_exhaustive_with_a_filter_and_limit(self):
        provider = FakeProvider(
            [
                _page([_review(NOW - timedelta(days=2), "old", rating=1)], "1"),
                _page([_review(NOW, "new", rating=1)], None),
            ],
            source="googleplay_official",
        )

        result = FakeClient(provider).fetch("123", ratings=[1], limit=1)

        assert [review.id for review in result.reviews] == ["new"]
        assert len(provider.calls) == 2

    @pytest.mark.parametrize(
        ("kwargs", "expected_ids", "expected_calls"),
        [
            ({"ratings": [1]}, ["new-low"], 1),
            ({"until": NOW - timedelta(hours=12)}, ["new-high"], 1),
            (
                {"ratings": [5], "until": NOW - timedelta(hours=12)},
                ["new-high"],
                1,
            ),
            ({"ratings": [1], "sort": Sort.OLDEST}, ["old-low"], 2),
            ({"sort": Sort.RATING}, ["new-high"], 2),
        ],
    )
    async def test_async_ordered_fetch_has_the_same_filter_aware_stop(
        self, kwargs, expected_ids, expected_calls
    ):
        provider = FakeProvider(
            [
                _page(
                    [
                        _review(NOW, "new-low", rating=1),
                        _review(NOW - timedelta(days=1), "new-high", rating=5),
                    ],
                    "1",
                ),
                _page([_review(NOW - timedelta(days=2), "old-low", rating=1)], None),
            ]
        )

        result = await FakeClient(provider).afetch(
            "123", countries=["us"], limit=1, **kwargs
        )

        assert [review.id for review in result.reviews] == expected_ids
        assert len(provider.calls) == expected_calls

    async def test_async_unordered_source_remains_exhaustive(self):
        provider = FakeProvider(
            [
                _page([_review(NOW - timedelta(days=2), "old", rating=1)], "1"),
                _page([_review(NOW, "new", rating=1)], None),
            ],
            source="googleplay_official",
        )

        result = await FakeClient(provider).afetch("123", ratings=[1], limit=1)

        assert [review.id for review in result.reviews] == ["new"]
        assert len(provider.calls) == 2

    @pytest.mark.parametrize("collision", ["max_pages", "cycle"])
    def test_satisfied_filtered_limit_outranks_safety_stop(self, collision):
        if collision == "max_pages":
            pages = [_page([_review(NOW, "match", rating=1)], "more")]
            max_pages = 1
        else:
            pages = [
                _page([_review(NOW, "skip", rating=5)], "1"),
                _page([_review(NOW - timedelta(days=1), "match", rating=1)], "1"),
            ]
            max_pages = None
        provider = FakeProvider(pages)

        result = FakeClient(provider).fetch(
            "123", ratings=[1], limit=1, max_pages=max_pages
        )

        assert [review.id for review in result.reviews] == ["match"]
        assert result.outcomes[0].stopped_because == "limit"

    @pytest.mark.parametrize("collision", ["max_pages", "cycle"])
    async def test_async_satisfied_filtered_limit_outranks_safety_stop(self, collision):
        if collision == "max_pages":
            pages = [_page([_review(NOW, "match", rating=1)], "more")]
            max_pages = 1
        else:
            pages = [
                _page([_review(NOW, "skip", rating=5)], "1"),
                _page([_review(NOW - timedelta(days=1), "match", rating=1)], "1"),
            ]
            max_pages = None
        provider = FakeProvider(pages)

        result = await FakeClient(provider).afetch(
            "123", ratings=[1], limit=1, max_pages=max_pages
        )

        assert [review.id for review in result.reviews] == ["match"]
        assert result.outcomes[0].stopped_because == "limit"


class TestEmptyCountries:
    """Omission and an explicit empty selection have different meanings.

    Omitting ``countries`` selects the provider default. Passing an empty or
    all-blank collection requests no storefronts and therefore performs no I/O;
    it must never silently become a US request.
    """

    def test_an_empty_countries_list_does_no_io(self):
        provider = FakeProvider([_page([_review(NOW, "a")], None)])

        result = FakeClient(provider).fetch("123", countries=[])

        assert result.reviews == []
        assert result.outcomes == []
        assert provider.calls == []

    def test_blank_countries_do_no_io(self):
        provider = FakeProvider([_page([_review(NOW, "a")], None)])

        result = FakeClient(provider).fetch("123", countries=["", "   "])

        assert result.reviews == []
        assert provider.calls == []


class TestRequestBounds:
    def test_default_concurrency_is_capped_at_eight(self):
        client = FakeClient(FakeProvider([]))

        assert client._workers([str(i) for i in range(155)], None) == 8

    def test_explicit_concurrency_override_is_honoured(self):
        client = FakeClient(FakeProvider([]))

        assert client._workers([str(i) for i in range(20)], 12) == 12

    @pytest.mark.parametrize(("concurrency", "expected"), [(None, 8), (3, 3)])
    def test_fetch_enforces_real_cross_country_concurrency(self, concurrency, expected):
        provider = BlockingInFlightProvider(expected)
        client = FakeClient(provider)

        with ThreadPoolExecutor(max_workers=1) as caller:
            future = caller.submit(
                client.fetch,
                "123",
                countries=Country.ALL,
                concurrency=concurrency,
            )
            try:
                assert provider.reached.wait(timeout=2)
                assert not provider.exceeded.wait(timeout=0.1)
            finally:
                provider.release.set()
            result = future.result(timeout=2)

        assert provider.maximum == expected
        assert len(result.outcomes) == len(Country.ALL)

    @pytest.mark.parametrize(("concurrency", "expected"), [(None, 8), (3, 3)])
    async def test_afetch_enforces_real_cross_country_concurrency(
        self, concurrency, expected
    ):
        provider = AsyncBlockingInFlightProvider(expected)
        task = asyncio.create_task(
            FakeClient(provider).afetch(
                "123", countries=Country.ALL, concurrency=concurrency
            )
        )
        try:
            await asyncio.wait_for(provider.reached.wait(), timeout=2)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(provider.exceeded.wait(), timeout=0.1)
        finally:
            provider.release.set()
        result = await asyncio.wait_for(task, timeout=2)

        assert provider.maximum == expected
        assert len(result.outcomes) == len(Country.ALL)

    def test_filtered_fetch_stops_at_public_max_pages_budget(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "r5-a", rating=5)], "1"),
                _page([_review(NOW, "r5-b", rating=5)], "2"),
                _page([_review(NOW, "r1", rating=1)], None),
            ]
        )

        result = FakeClient(provider).fetch(
            "123", countries=["us"], ratings=[1], limit=1, max_pages=2
        )

        assert result.reviews == []
        assert len(provider.calls) == 2
        assert result.outcomes[0].stopped_because == "max_pages"

    async def test_async_filtered_fetch_has_the_same_max_pages_budget(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "r5-a", rating=5)], "1"),
                _page([_review(NOW, "r5-b", rating=5)], "2"),
                _page([_review(NOW, "r1", rating=1)], None),
            ]
        )

        result = await FakeClient(provider).afetch(
            "123", countries=["us"], ratings=[1], limit=1, max_pages=2
        )

        assert result.reviews == []
        assert len(provider.calls) == 2
        assert result.outcomes[0].stopped_because == "max_pages"

    def test_zero_limit_does_no_io(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        result = FakeClient(provider).fetch("123", limit=0)

        assert result.reviews == []
        assert result.outcomes == []
        assert provider.calls == []

    def test_negative_limit_is_rejected_before_io(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        import pytest

        with pytest.raises(ValueError, match="limit"):
            FakeClient(provider).fetch("123", limit=-1)
        assert provider.calls == []

    async def test_async_zero_limit_does_no_io(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        result = await FakeClient(provider).afetch("123", limit=0)

        assert result.reviews == []
        assert result.outcomes == []
        assert provider.calls == []

    async def test_async_empty_countries_do_no_io(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        result = await FakeClient(provider).afetch("123", countries=[])

        assert result.reviews == []
        assert result.outcomes == []
        assert provider.calls == []

    @pytest.mark.parametrize("concurrency", [0, -1])
    def test_invalid_concurrency_is_rejected_before_provider_io(self, concurrency):
        provider = FakeProvider([_page([_review(NOW)], None)])
        client = FakeClient(provider)
        client._ensure_provider = pytest.fail

        with pytest.raises(ValueError, match="concurrency"):
            client.fetch("123", concurrency=concurrency)
        assert provider.calls == []

    @pytest.mark.parametrize("concurrency", [0, -1])
    async def test_async_invalid_concurrency_is_rejected_before_provider_io(
        self, concurrency
    ):
        provider = FakeProvider([_page([_review(NOW)], None)])
        client = FakeClient(provider)

        async def fail_provider_build():
            pytest.fail("invalid concurrency reached provider construction")

        client._aensure_provider = fail_provider_build

        with pytest.raises(ValueError, match="concurrency"):
            await client.afetch("123", concurrency=concurrency)
        assert provider.calls == []

    def test_negative_max_pages_is_rejected_before_provider_io(self):
        provider = FakeProvider([_page([_review(NOW)], None)])
        client = FakeClient(provider)
        client._ensure_provider = pytest.fail

        with pytest.raises(ValueError, match="max_pages"):
            client.fetch("123", max_pages=-1)
        assert provider.calls == []

    async def test_async_negative_max_pages_is_rejected_before_provider_io(self):
        provider = FakeProvider([_page([_review(NOW)], None)])
        client = FakeClient(provider)

        async def fail_provider_build():
            pytest.fail("invalid max_pages reached provider construction")

        client._aensure_provider = fail_provider_build

        with pytest.raises(ValueError, match="max_pages"):
            await client.afetch("123", max_pages=-1)
        assert provider.calls == []


class TestCountryNormalisation:
    """The fan-out normalises and dedupes before it walks.

    Every entry in the resolved list costs one full walk, so a list that names
    the same storefront twice (directly, in another case, or in the alpha-3
    alphabet) must collapse to one. Otherwise the same reviews come back
    twice and ``outcomes`` reports one storefront under two names.
    """

    def test_a_repeated_country_is_walked_once(self):
        provider = MultiCountryProvider({"us": [_page([_review(NOW, "a")], None)]})

        result = FakeClient(provider).fetch("123", countries=["us", "us"])

        assert [r.id for r in result.reviews] == ["a"]
        assert [o.country for o in result.outcomes] == ["us"]
        assert provider.calls == [("us", None)]

    def test_case_variants_collapse_to_one_walk(self):
        provider = MultiCountryProvider({"us": [_page([_review(NOW, "a")], None)]})

        result = FakeClient(provider).fetch("123", countries=["US", "us"])

        assert [r.id for r in result.reviews] == ["a"]
        assert [o.country for o in result.outcomes] == ["us"]

    def test_alpha3_and_alpha2_for_one_storefront_collapse(self):
        provider = MultiCountryProvider({"us": [_page([_review(NOW, "a")], None)]})

        result = FakeClient(provider).fetch("123", countries=["USA", "us"])

        assert [o.country for o in result.outcomes] == ["us"]

    def test_surrounding_whitespace_is_stripped(self):
        provider = MultiCountryProvider({"gb": [_page([_review(NOW, "a")], None)]})

        result = FakeClient(provider).fetch("123", countries=["  gb  "])

        assert [r.id for r in result.reviews] == ["a"]
        assert [o.country for o in result.outcomes] == ["gb"]

    def test_dedup_preserves_first_appearance_order(self):
        client = FakeClient(FakeProvider([]))

        assert client.resolve_countries(["gb", "us", "gb", "de"]) == ["gb", "us", "de"]

    def test_country_enum_members_are_accepted(self):
        client = FakeClient(FakeProvider([]))

        assert client.resolve_countries([Country.US, Country.GB]) == ["us", "gb"]

    def test_a_region_group_frozenset_is_accepted(self):
        """``Country.ALL`` and friends are frozensets, not lists."""
        client = FakeClient(FakeProvider([]))

        resolved = client.resolve_countries(Country.MIDDLE_EAST)

        assert sorted(resolved) == sorted(c.value for c in Country.MIDDLE_EAST)

    def test_an_unrecognised_code_is_kept_and_logged(self, caplog):
        """Same contract as ``normalise_country``: an odd storefront beats a
        dropped one, but it is logged so a typo is findable."""
        import logging

        client = FakeClient(FakeProvider([]))

        with caplog.at_level(logging.WARNING):
            assert client.resolve_countries(["zz"]) == ["zz"]

        assert "Unrecognised storefront" in caplog.text


class TestAsyncParity:
    async def test_afetch_matches_fetch(self):
        def build():
            return MultiCountryProvider(
                {
                    "us": [_page([_review(NOW, "us1")], None)],
                    "gb": [_page([_review(NOW, "gb1")], None)],
                }
            )

        sync_result = FakeClient(build()).fetch("123", countries=["us", "gb"])
        async_provider = build()
        async_result = await FakeClient(async_provider).afetch(
            "123", countries=["us", "gb"]
        )

        assert {r.id for r in sync_result.reviews} == {
            r.id for r in async_result.reviews
        }
        assert {o.country for o in sync_result.outcomes} == {
            o.country for o in async_result.outcomes
        }
        # Proves afetch actually drove afetch_page, not fetch_page.
        assert async_provider.async_calls == async_provider.calls
        assert len(async_provider.async_calls) == 2

    async def test_afetch_walks_multiple_pages_with_no_limit(self):
        """Mirrors TestSingleCountry.test_outcome_records_page_count, async."""
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                _page([_review(NOW, "b")], "2"),
                _page([_review(NOW, "c")], None),
            ]
        )
        result = await FakeClient(provider).afetch("123", countries=["us"])

        assert result.outcomes[0].pages == 3
        assert [r.id for r in result.reviews] == ["a", "b", "c"]

    async def test_afetch_reports_stop_reasons(self):
        provider = FakeProvider(
            [_page([_review(NOW, "a"), _review(NOW, "b")], "1"), _page([], None)]
        )
        result = await FakeClient(provider).afetch("123", countries=["us"], limit=2)

        assert result.outcomes[0].stopped_because == "limit"

    async def test_afetch_honours_concurrency_one(self):
        provider = MultiCountryProvider(
            {
                "us": [_page([_review(NOW, "us1")], None)],
                "gb": [_page([_review(NOW, "gb1")], None)],
            }
        )
        result = await FakeClient(provider).afetch(
            "123", countries=["us", "gb"], concurrency=1
        )

        assert len(result.reviews) == 2
