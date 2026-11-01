"""Tests for the full fetch: rung 3 of the client ladder."""

import asyncio
from datetime import timedelta
from threading import Event, Lock
from typing import Literal

import pytest

from app_reviews.models.country import Country
from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError, FetchResult
from app_reviews.models.types import Sort
from tests.app_reviews.factories import (
    NOW,
    FakeClient,
    FakeProvider,
    MultiCountryProvider,
    make_page,
    make_review,
)


class _Signal:
    """A flag a worker thread or a task can set and either one can wait on.

    The async wait polls, so one signal serves a provider that is driven from
    threads (sync ``fetch``) and from the event loop (``afetch``) alike.
    """

    _POLL_SECONDS: float = 0.005

    def __init__(self) -> None:
        self._flag: Event = Event()

    def set(self) -> None:
        self._flag.set()

    def wait(self, timeout: float) -> bool:
        return self._flag.wait(timeout)

    async def wait_async(self, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while not self._flag.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(self._POLL_SECONDS)
        return True


class BlockingInFlightProvider:
    """Hold requests open so a public fetch's real fan-out is observable.

    One provider serves both entry points. ``path`` names the one the test
    drives (``"sync"`` or ``"async"``); the other raises, so a walk that
    silently fell back to the wrong twin fails the test.
    """

    source = "appstore_scraper"
    _BLOCK_SECONDS: float = 2

    def __init__(self, expected: int, path: Literal["sync", "async"]):
        self.expected = expected
        self.path: Literal["sync", "async"] = path
        self.active = 0
        self.maximum = 0
        self.lock: Lock = Lock()
        self.reached: _Signal = _Signal()
        self.exceeded: _Signal = _Signal()
        self.release: _Signal = _Signal()

    def fetch_page(self, app_id, country, cursor):
        self._require_path("sync")
        self._enter()
        try:
            if not self.release.wait(self._BLOCK_SECONDS):
                raise AssertionError("fetch did not release blocked provider calls")
        finally:
            self._leave()
        return PageResult()

    async def afetch_page(self, app_id, country, cursor):
        self._require_path("async")
        self._enter()
        try:
            if not await self.release.wait_async(self._BLOCK_SECONDS):
                raise AssertionError("afetch did not release blocked provider calls")
        finally:
            self._leave()
        return PageResult()

    def _require_path(self, entry: str) -> None:
        if entry != self.path:
            raise AssertionError(f"{self.path} concurrency test used the {entry} path")

    def _enter(self) -> None:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active >= self.expected:
                self.reached.set()
            if self.active > self.expected:
                self.exceeded.set()

    def _leave(self) -> None:
        with self.lock:
            self.active -= 1


class TestSingleCountry:
    def test_collects_reviews_across_pages(self):
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="a")], "1"),
                make_page([make_review(created_at=NOW, id="b")], None),
            ]
        )
        result = FakeClient(provider).fetch("123", countries=["us"])

        assert [r.id for r in result.reviews] == ["a", "b"]

    def test_reports_one_outcome_per_country(self):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])

        result = FakeClient(provider).fetch("123", countries=["us"])

        assert len(result.outcomes) == 1
        assert result.outcomes[0].country == "us"
        assert result.outcomes[0].pages == 1
        assert result.outcomes[0].reviews_fetched == 1
        assert result.outcomes[0].stopped_because == "exhausted"

    def test_outcome_records_page_count(self):
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="a")], "1"),
                make_page([make_review(created_at=NOW, id="b")], "2"),
                make_page([make_review(created_at=NOW, id="c")], None),
            ]
        )
        result = FakeClient(provider).fetch("123", countries=["us"])

        assert result.outcomes[0].pages == 3

    def test_actual_walk_aggregates_skipped_reviews_into_the_result_envelope(self):
        provider = FakeProvider(
            [
                PageResult(
                    reviews=[make_review(created_at=NOW, id="a")],
                    next_cursor="1",
                    skipped_reviews=2,
                ),
                PageResult(
                    reviews=[make_review(created_at=NOW, id="b")],
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
                make_page(
                    [
                        make_review(created_at=NOW, id="a"),
                        make_review(created_at=NOW, id="b"),
                    ],
                    "1",
                ),
                make_page([make_review(created_at=NOW, id="c")], None),
            ]
        )
        result = FakeClient(provider).fetch("123", countries=["us"], limit=2)

        assert result.outcomes[0].stopped_because == "limit"
        assert len(result.reviews) == 2

    def test_exhausted_when_the_provider_runs_out(self):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])

        result = FakeClient(provider).fetch("123", countries=["us"], limit=100)

        assert result.outcomes[0].stopped_because == "exhausted"

    def test_since_is_reported_as_the_stop_reason(self):
        old = NOW - timedelta(days=30)
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="new")], "1"),
                make_page([make_review(created_at=old, id="old")], "2"),
            ]
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
            [
                make_page([make_review(created_at=NOW, id="a")], "1"),
                PageResult(error=error),
            ]
        )

        result = FakeClient(provider).fetch("123", countries=["us"])

        assert [r.id for r in result.reviews] == ["a"]
        assert result.errors == [error]
        assert result.outcomes[0].stopped_because == "error"


class TestMultipleCountries:
    def test_merges_reviews_from_every_country(self):
        provider = MultiCountryProvider(
            {
                "us": [make_page([make_review(created_at=NOW, id="us1")], None)],
                "gb": [make_page([make_review(created_at=NOW, id="gb1")], None)],
            }
        )
        result = FakeClient(provider).fetch("123", countries=["us", "gb"])

        assert {r.id for r in result.reviews} == {"us1", "gb1"}

    def test_one_outcome_per_country(self):
        provider = MultiCountryProvider(
            {
                "us": [make_page([make_review(created_at=NOW, id="us1")], None)],
                "gb": [make_page([make_review(created_at=NOW, id="gb1")], None)],
            }
        )
        result = FakeClient(provider).fetch("123", countries=["us", "gb"])

        assert {o.country for o in result.outcomes} == {"us", "gb"}

    def test_one_country_failing_does_not_lose_the_others(self):
        provider = MultiCountryProvider(
            {
                "us": [make_page([make_review(created_at=NOW, id="us1")], None)],
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
                "us": [make_page([make_review(created_at=NOW, id="us1")], None)],
                "gb": [make_page([make_review(created_at=NOW, id="gb1")], None)],
            }
        )
        result = FakeClient(provider).fetch(
            "123", countries=["us", "gb"], concurrency=1
        )

        assert len(result.reviews) == 2
        assert [c[0] for c in provider.calls] == ["us", "gb"]

    def test_global_provider_makes_one_call_regardless_of_countries(self):
        provider = FakeProvider(
            [make_page([make_review(created_at=NOW)], None)],
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
                make_page(
                    [
                        make_review(created_at=NOW, id="a"),
                        make_review(created_at=NOW, id="b"),
                    ],
                    "1",
                ),
                make_page([make_review(created_at=NOW, id="c")], "2"),
            ]
        )
        FakeClient(provider).fetch("123", countries=["us"], sort=Sort.NEWEST, limit=2)

        assert len(provider.calls) == 1

    def test_rating_with_limit_exhausts_pagination_first(self):
        newest_low = make_review(created_at=NOW, id="newest_low", rating=1)
        oldest_high = make_review(
            created_at=NOW - timedelta(days=5), id="oldest_high", rating=5
        )

        provider = FakeProvider(
            [make_page([newest_low], "1"), make_page([oldest_high], None)]
        )
        result = FakeClient(provider).fetch(
            "123", countries=["us"], sort=Sort.RATING, limit=1
        )

        assert len(provider.calls) == 2
        assert [r.id for r in result.reviews] == ["oldest_high"]

    def test_oldest_with_limit_exhausts_pagination_first(self):
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="newest")], "1"),
                make_page(
                    [make_review(created_at=NOW - timedelta(days=10), id="oldest")],
                    None,
                ),
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
                make_page(
                    [make_review(created_at=NOW - timedelta(days=10), id="old")], "1"
                ),
                make_page([make_review(created_at=NOW, id="new")], None),
            ],
            source="googleplay_official",
        )
        result = FakeClient(provider).fetch("123", sort=Sort.NEWEST, limit=1)

        assert len(provider.calls) == 2
        assert [r.id for r in result.reviews] == ["new"]


class TestFilters:
    def test_fetch_filters_each_page_instead_of_retaining_then_refiltering(
        self, monkeypatch
    ):
        def late_filter(*args, **kwargs):
            raise AssertionError("fetch retained raw reviews until final assembly")

        monkeypatch.setattr(FetchResult, "filter", late_filter)
        provider = FakeProvider(
            [
                make_page(
                    [
                        make_review(created_at=NOW, id="keep", rating=1),
                        make_review(created_at=NOW, id="drop"),
                    ],
                    None,
                )
            ]
        )

        result = FakeClient(provider).fetch("123", ratings=[1])

        assert [review.id for review in result.reviews] == ["keep"]

    def test_until_filters_the_returned_set(self):
        provider = FakeProvider(
            [
                make_page(
                    [
                        make_review(created_at=NOW, id="new"),
                        make_review(created_at=NOW - timedelta(days=10), id="old"),
                    ],
                    None,
                )
            ]
        )
        result = FakeClient(provider).fetch(
            "123", countries=["us"], until=NOW - timedelta(days=5)
        )

        assert [r.id for r in result.reviews] == ["old"]

    def test_since_filters_the_boundary_exactly(self):
        old = make_review(created_at=NOW - timedelta(days=10), id="old")
        provider = FakeProvider(
            [make_page([make_review(created_at=NOW, id="new"), old], None)]
        )
        result = FakeClient(provider).fetch(
            "123", countries=["us"], since=NOW - timedelta(days=2)
        )

        assert [r.id for r in result.reviews] == ["new"]

    def test_ratings_filter(self):
        low = make_review(created_at=NOW, id="low", rating=1)
        provider = FakeProvider(
            [make_page([make_review(created_at=NOW, id="high"), low], None)]
        )

        result = FakeClient(provider).fetch("123", countries=["us"], ratings=[1])

        assert [r.id for r in result.reviews] == ["low"]

    def test_outcome_counts_wire_rows_while_only_matches_are_retained(self):
        provider = FakeProvider(
            [
                make_page(
                    [
                        make_review(created_at=NOW, id="keep", rating=1),
                        make_review(created_at=NOW, id="drop-rating", rating=5),
                        make_review(
                            created_at=NOW - timedelta(days=20),
                            id="drop-date",
                            rating=1,
                        ),
                    ],
                    None,
                )
            ]
        )

        result = FakeClient(provider).fetch(
            "123", ratings=[1], since=NOW - timedelta(days=2)
        )

        assert [review.id for review in result.reviews] == ["keep"]
        assert result.outcomes[0].reviews_fetched == 3

    async def test_async_outcome_counts_wire_rows_without_retaining_rejections(self):
        provider = FakeProvider(
            [
                make_page(
                    [
                        make_review(created_at=NOW, id="keep", rating=1),
                        make_review(created_at=NOW, id="drop", rating=5),
                    ],
                    None,
                )
            ]
        )

        result = await FakeClient(provider).afetch("123", ratings=[1])

        assert [review.id for review in result.reviews] == ["keep"]
        assert result.outcomes[0].reviews_fetched == 2

    def test_each_country_counts_its_wire_rows_before_filtering(self):
        provider = MultiCountryProvider(
            {
                "us": [
                    make_page(
                        [
                            make_review(created_at=NOW, id="us-keep", rating=1),
                            make_review(created_at=NOW, id="us-drop", rating=5),
                        ],
                        None,
                    )
                ],
                "gb": [
                    make_page(
                        [make_review(created_at=NOW, id="gb-drop", rating=5)], None
                    )
                ],
            }
        )

        result = FakeClient(provider).fetch(
            "123", countries=["us", "gb"], ratings=[1], concurrency=1
        )

        assert [review.id for review in result.reviews] == ["us-keep"]
        accounting = [
            (outcome.country, outcome.reviews_fetched) for outcome in result.outcomes
        ]
        assert accounting == [
            ("us", 2),
            ("gb", 1),
        ]


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
                make_page(
                    [
                        make_review(created_at=NOW, id="new1"),
                        make_review(created_at=NOW, id="new2"),
                        make_review(created_at=NOW, id="new3"),
                        make_review(created_at=NOW, id="new4"),
                    ],
                    "1",
                ),
                make_page(
                    [
                        make_review(created_at=NOW - timedelta(days=12), id="old1"),
                        make_review(created_at=NOW - timedelta(days=13), id="old2"),
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
                make_page(
                    [
                        make_review(created_at=NOW, id="r5_a", rating=5),
                        make_review(created_at=NOW, id="r5_b", rating=5),
                    ],
                    "1",
                ),
                make_page(
                    [
                        make_review(created_at=NOW, id="r1_a", rating=1),
                        make_review(created_at=NOW, id="r1_b", rating=1),
                    ],
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
                make_page([make_review(created_at=NOW, id="too_new")], "1"),
                make_page(
                    [
                        make_review(created_at=NOW - timedelta(days=6), id="old1"),
                        make_review(created_at=NOW - timedelta(days=7), id="old2"),
                        make_review(created_at=NOW - timedelta(days=8), id="old3"),
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
                make_page(
                    [
                        make_review(created_at=NOW, id="a"),
                        make_review(created_at=NOW, id="b"),
                    ],
                    "1",
                ),
                make_page([make_review(created_at=NOW, id="c")], "2"),
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
                make_page(
                    [
                        make_review(created_at=NOW, id="new-low", rating=1),
                        make_review(
                            created_at=NOW - timedelta(days=1), id="new-high", rating=5
                        ),
                    ],
                    "1",
                ),
                make_page(
                    [
                        make_review(
                            created_at=NOW - timedelta(days=2), id="old-low", rating=1
                        )
                    ],
                    None,
                ),
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
                make_page(
                    [
                        make_review(
                            created_at=NOW - timedelta(days=2), id="old", rating=1
                        )
                    ],
                    "1",
                ),
                make_page([make_review(created_at=NOW, id="new", rating=1)], None),
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
                make_page(
                    [
                        make_review(created_at=NOW, id="new-low", rating=1),
                        make_review(
                            created_at=NOW - timedelta(days=1), id="new-high", rating=5
                        ),
                    ],
                    "1",
                ),
                make_page(
                    [
                        make_review(
                            created_at=NOW - timedelta(days=2), id="old-low", rating=1
                        )
                    ],
                    None,
                ),
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
                make_page(
                    [
                        make_review(
                            created_at=NOW - timedelta(days=2), id="old", rating=1
                        )
                    ],
                    "1",
                ),
                make_page([make_review(created_at=NOW, id="new", rating=1)], None),
            ],
            source="googleplay_official",
        )

        result = await FakeClient(provider).afetch("123", ratings=[1], limit=1)

        assert [review.id for review in result.reviews] == ["new"]
        assert len(provider.calls) == 2

    @pytest.mark.parametrize("collision", ["max_pages", "cycle"])
    def test_satisfied_filtered_limit_outranks_safety_stop(self, collision):
        if collision == "max_pages":
            pages = [
                make_page([make_review(created_at=NOW, id="match", rating=1)], "more")
            ]
            max_pages = 1
        else:
            pages = [
                make_page([make_review(created_at=NOW, id="skip", rating=5)], "1"),
                make_page(
                    [
                        make_review(
                            created_at=NOW - timedelta(days=1), id="match", rating=1
                        )
                    ],
                    "1",
                ),
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
            pages = [
                make_page([make_review(created_at=NOW, id="match", rating=1)], "more")
            ]
            max_pages = 1
        else:
            pages = [
                make_page([make_review(created_at=NOW, id="skip", rating=5)], "1"),
                make_page(
                    [
                        make_review(
                            created_at=NOW - timedelta(days=1), id="match", rating=1
                        )
                    ],
                    "1",
                ),
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
        provider = FakeProvider(
            [make_page([make_review(created_at=NOW, id="a")], None)]
        )

        result = FakeClient(provider).fetch("123", countries=[])

        assert result.reviews == []
        assert result.outcomes == []
        assert provider.calls == []

    def test_blank_countries_do_no_io(self):
        provider = FakeProvider(
            [make_page([make_review(created_at=NOW, id="a")], None)]
        )

        result = FakeClient(provider).fetch("123", countries=["", "   "])

        assert result.reviews == []
        assert provider.calls == []


class TestRequestBounds:
    @pytest.mark.parametrize(("concurrency", "expected"), [(None, 8), (3, 3)])
    async def test_fetch_enforces_real_cross_country_concurrency(
        self, twin, concurrency, expected
    ):
        provider = BlockingInFlightProvider(expected, path=twin.mode)
        walk = twin.start(
            FakeClient(provider),
            "fetch",
            "123",
            countries=Country.ALL,
            concurrency=concurrency,
        )
        try:
            assert await provider.reached.wait_async(timeout=2)
            assert not await provider.exceeded.wait_async(timeout=0.1)
        finally:
            provider.release.set()
        result = await asyncio.wait_for(walk, timeout=2)

        assert provider.maximum == expected
        assert len(result.outcomes) == len(Country.ALL)

    def test_filtered_fetch_stops_at_public_max_pages_budget(self):
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="r5-a", rating=5)], "1"),
                make_page([make_review(created_at=NOW, id="r5-b", rating=5)], "2"),
                make_page([make_review(created_at=NOW, id="r1", rating=1)], None),
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
                make_page([make_review(created_at=NOW, id="r5-a", rating=5)], "1"),
                make_page([make_review(created_at=NOW, id="r5-b", rating=5)], "2"),
                make_page([make_review(created_at=NOW, id="r1", rating=1)], None),
            ]
        )

        result = await FakeClient(provider).afetch(
            "123", countries=["us"], ratings=[1], limit=1, max_pages=2
        )

        assert result.reviews == []
        assert len(provider.calls) == 2
        assert result.outcomes[0].stopped_because == "max_pages"

    def test_zero_limit_does_no_io(self):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])

        result = FakeClient(provider).fetch("123", limit=0)

        assert result.reviews == []
        assert result.outcomes == []
        assert provider.calls == []

    def test_negative_limit_is_rejected_before_io(self):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])

        import pytest

        with pytest.raises(ValueError, match="limit"):
            FakeClient(provider).fetch("123", limit=-1)
        assert provider.calls == []

    async def test_async_zero_limit_does_no_io(self):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])

        result = await FakeClient(provider).afetch("123", limit=0)

        assert result.reviews == []
        assert result.outcomes == []
        assert provider.calls == []

    async def test_async_empty_countries_do_no_io(self):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])

        result = await FakeClient(provider).afetch("123", countries=[])

        assert result.reviews == []
        assert result.outcomes == []
        assert provider.calls == []

    @pytest.mark.parametrize("concurrency", [0, -1])
    def test_invalid_concurrency_is_rejected_before_provider_io(self, concurrency):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])
        client = FakeClient(provider)
        client._ensure_provider = pytest.fail

        with pytest.raises(ValueError, match="concurrency"):
            client.fetch("123", concurrency=concurrency)
        assert provider.calls == []

    @pytest.mark.parametrize("concurrency", [0, -1])
    async def test_async_invalid_concurrency_is_rejected_before_provider_io(
        self, concurrency
    ):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])
        client = FakeClient(provider)

        async def fail_provider_build():
            pytest.fail("invalid concurrency reached provider construction")

        client._aensure_provider = fail_provider_build

        with pytest.raises(ValueError, match="concurrency"):
            await client.afetch("123", concurrency=concurrency)
        assert provider.calls == []

    def test_negative_max_pages_is_rejected_before_provider_io(self):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])
        client = FakeClient(provider)
        client._ensure_provider = pytest.fail

        with pytest.raises(ValueError, match="max_pages"):
            client.fetch("123", max_pages=-1)
        assert provider.calls == []

    async def test_async_negative_max_pages_is_rejected_before_provider_io(self):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])
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
        provider = MultiCountryProvider(
            {"us": [make_page([make_review(created_at=NOW, id="a")], None)]}
        )

        result = FakeClient(provider).fetch("123", countries=["us", "us"])

        assert [r.id for r in result.reviews] == ["a"]
        assert [o.country for o in result.outcomes] == ["us"]
        assert provider.calls == [("us", None)]

    def test_case_variants_collapse_to_one_walk(self):
        provider = MultiCountryProvider(
            {"us": [make_page([make_review(created_at=NOW, id="a")], None)]}
        )

        result = FakeClient(provider).fetch("123", countries=["US", "us"])

        assert [r.id for r in result.reviews] == ["a"]
        assert [o.country for o in result.outcomes] == ["us"]

    def test_alpha3_and_alpha2_for_one_storefront_collapse(self):
        provider = MultiCountryProvider(
            {"us": [make_page([make_review(created_at=NOW, id="a")], None)]}
        )

        result = FakeClient(provider).fetch("123", countries=["USA", "us"])

        assert [o.country for o in result.outcomes] == ["us"]

    def test_surrounding_whitespace_is_stripped(self):
        provider = MultiCountryProvider(
            {"gb": [make_page([make_review(created_at=NOW, id="a")], None)]}
        )

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
                    "us": [make_page([make_review(created_at=NOW, id="us1")], None)],
                    "gb": [make_page([make_review(created_at=NOW, id="gb1")], None)],
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
                make_page([make_review(created_at=NOW, id="a")], "1"),
                make_page([make_review(created_at=NOW, id="b")], "2"),
                make_page([make_review(created_at=NOW, id="c")], None),
            ]
        )
        result = await FakeClient(provider).afetch("123", countries=["us"])

        assert result.outcomes[0].pages == 3
        assert [r.id for r in result.reviews] == ["a", "b", "c"]

    async def test_afetch_reports_stop_reasons(self):
        provider = FakeProvider(
            [
                make_page(
                    [
                        make_review(created_at=NOW, id="a"),
                        make_review(created_at=NOW, id="b"),
                    ],
                    "1",
                ),
                make_page([], None),
            ]
        )
        result = await FakeClient(provider).afetch("123", countries=["us"], limit=2)

        assert result.outcomes[0].stopped_because == "limit"

    async def test_afetch_honours_concurrency_one(self):
        provider = MultiCountryProvider(
            {
                "us": [make_page([make_review(created_at=NOW, id="us1")], None)],
                "gb": [make_page([make_review(created_at=NOW, id="gb1")], None)],
            }
        )
        result = await FakeClient(provider).afetch(
            "123", countries=["us", "gb"], concurrency=1
        )

        assert len(result.reviews) == 2
