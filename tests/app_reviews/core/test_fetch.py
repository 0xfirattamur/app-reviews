"""Tests for the full fetch: rung 3 of the client ladder."""

from datetime import timedelta

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
    """FIX: `limit` must not bound the walk when `ratings`/`until` are also
    requested, because the walk cannot know in advance how many of the first
    `limit` unfiltered reviews will survive filtering, so bounding at
    `limit` can return fewer results than exist, including zero."""

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


class TestEmptyCountries:
    """The resolved fan-out can no longer be empty.

    It used to be reachable only via a provider whose ``countries()`` returned
    ``[]``. With the fan-out derived from ``core.paging.is_per_country`` it is
    either ``[""]`` or ``countries or ["us"]``, never empty, so the
    ``if not resolved`` guard in ``fetch``/``afetch`` went with it.
    """

    def test_an_empty_countries_list_falls_back_to_the_default(self):
        provider = FakeProvider([_page([_review(NOW, "a")], None)])

        result = FakeClient(provider).fetch("123", countries=[])

        assert [r.id for r in result.reviews] == ["a"]
        assert [o.country for o in result.outcomes] == ["us"]


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
