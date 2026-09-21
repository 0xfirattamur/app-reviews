"""Tests for FetchResult."""

import json
from datetime import UTC, date, datetime

import pytest

from app_reviews.models.result import CountryOutcome, FetchError, FetchResult
from app_reviews.models.types import Sort
from tests.app_reviews.factories import make_review


def test_fetch_result_default_empty():
    result = FetchResult()
    assert result.reviews == []
    assert result.errors == []


def test_fetch_result_iter():
    r1 = make_review(id="1")
    r2 = make_review(id="2")
    result = FetchResult(reviews=[r1, r2])
    assert list(result) == [r1, r2]


def test_fetch_result_len():
    result = FetchResult(reviews=[make_review(id="1"), make_review(id="2")])
    assert len(result) == 2


def test_fetch_result_bool_truthy():
    assert bool(FetchResult(reviews=[make_review()])) is True


def test_fetch_result_bool_falsy():
    assert bool(FetchResult()) is False


def test_filter_by_ratings():
    r1 = make_review(id="1", rating=5)
    r2 = make_review(id="2", rating=2)
    result = FetchResult(reviews=[r1, r2]).filter(ratings=[4, 5])
    assert len(result) == 1
    assert next(iter(result)).rating == 5


def test_filter_by_since():
    old = make_review(id="1", created_at=datetime(2024, 1, 1, tzinfo=UTC))
    new = make_review(id="2", created_at=datetime(2025, 6, 1, tzinfo=UTC))
    result = FetchResult(reviews=[old, new]).filter(since=date(2025, 1, 1))
    assert len(result) == 1


def test_filter_by_until():
    old = make_review(id="1", created_at=datetime(2024, 1, 1, tzinfo=UTC))
    new = make_review(id="2", created_at=datetime(2025, 6, 1, tzinfo=UTC))
    result = FetchResult(reviews=[old, new]).filter(until=date(2025, 1, 1))
    assert len(result) == 1
    assert next(iter(result)).id == "1"


def test_filter_by_date_until_includes_the_entire_day():
    during_day = make_review(
        id="1", created_at=datetime(2025, 1, 1, 23, 59, 59, 999999, tzinfo=UTC)
    )
    next_day = make_review(id="2", created_at=datetime(2025, 1, 2, tzinfo=UTC))

    result = FetchResult(reviews=[during_day, next_day]).filter(until=date(2025, 1, 1))

    assert [review.id for review in result] == ["1"]


def test_filter_is_non_destructive():
    r1 = make_review(id="1", rating=5)
    r2 = make_review(id="2", rating=2)
    original = FetchResult(reviews=[r1, r2])
    filtered = original.filter(ratings=[5])
    assert len(original) == 2
    assert len(filtered) == 1


def test_sort_newest():
    old = make_review(id="1", created_at=datetime(2024, 1, 1, tzinfo=UTC))
    new = make_review(id="2", created_at=datetime(2025, 6, 1, tzinfo=UTC))
    result = FetchResult(reviews=[old, new]).sort(Sort.NEWEST)
    assert next(iter(result)).id == "2"


def test_sort_oldest():
    old = make_review(id="1", created_at=datetime(2024, 1, 1, tzinfo=UTC))
    new = make_review(id="2", created_at=datetime(2025, 6, 1, tzinfo=UTC))
    result = FetchResult(reviews=[old, new]).sort(Sort.OLDEST)
    assert next(iter(result)).id == "1"


def test_sort_rating():
    low = make_review(id="1", rating=1)
    high = make_review(id="2", rating=5)
    result = FetchResult(reviews=[low, high]).sort(Sort.RATING)
    assert next(iter(result)).rating == 5


def test_limit_truncates():
    reviews = [make_review(id=str(i)) for i in range(10)]
    result = FetchResult(reviews=reviews).limit(3)
    assert len(result) == 3


def test_limit_none_is_noop():
    reviews = [make_review(id=str(i)) for i in range(5)]
    result = FetchResult(reviews=reviews).limit(None)
    assert len(result) == 5


def test_limit_larger_than_count_is_noop():
    reviews = [make_review(id=str(i)) for i in range(3)]
    result = FetchResult(reviews=reviews).limit(100)
    assert len(result) == 3


def test_negative_limit_is_rejected():
    with pytest.raises(ValueError, match="limit"):
        FetchResult(reviews=[]).limit(-1)


def test_chain_filter_sort_limit():
    reviews = [
        make_review(id="1", rating=5, created_at=datetime(2025, 1, 1, tzinfo=UTC)),
        make_review(id="2", rating=1, created_at=datetime(2025, 6, 1, tzinfo=UTC)),
        make_review(id="3", rating=5, created_at=datetime(2024, 1, 1, tzinfo=UTC)),
    ]
    result = FetchResult(reviews=reviews).filter(ratings=[5]).sort(Sort.NEWEST).limit(1)
    assert len(result) == 1
    assert next(iter(result)).id == "1"


class TestToDicts:
    """Delegates to ``Review.to_dict`` so there is one conversion, not two."""

    def test_converts_each_review(self):
        r = make_review(id="1", rating=4, title="Nice", body="Good app")
        dicts = FetchResult(reviews=[r]).to_dicts()

        assert len(dicts) == 1
        assert dicts[0]["rating"] == 4
        assert dicts[0]["title"] == "Nice"

    def test_excludes_raw_by_default(self):
        r = make_review(raw={"entry": ["payload"]})

        assert "raw" not in FetchResult(reviews=[r]).to_dicts()[0]

    def test_include_raw_reaches_through(self):
        r = make_review(raw={"entry": ["payload"]})

        assert FetchResult(reviews=[r]).to_dicts(include_raw=True)[0]["raw"] == {
            "entry": ["payload"]
        }

    def test_the_output_is_json_serialisable(self):
        """`json.dumps(result.to_dicts())` raised TypeError on the datetimes,
        which is why callers needed the exporters at all."""
        result = FetchResult(reviews=[make_review(), make_review(id="2")])

        assert len(json.loads(json.dumps(result.to_dicts()))) == 2


def _outcome(country="us", stopped_because="exhausted"):
    return CountryOutcome(
        country=country,
        pages=3,
        reviews_fetched=120,
        stopped_because=stopped_because,
        elapsed=0.5,
    )


class TestCountryOutcome:
    def test_error_defaults_to_none(self):
        assert _outcome().error is None

    def test_elapsed_is_whatever_the_walk_measured(self):
        assert _outcome().elapsed == 0.5

    @pytest.mark.parametrize(
        "elapsed", [float("nan"), float("inf"), float("-inf"), -0.001]
    )
    def test_elapsed_must_be_finite_and_non_negative(self, elapsed):
        with pytest.raises(ValueError, match="elapsed must be finite and non-negative"):
            CountryOutcome(
                country="us",
                pages=1,
                reviews_fetched=1,
                stopped_because="exhausted",
                elapsed=elapsed,
            )

    def test_skipped_reviews_defaults_to_zero(self):
        assert _outcome().skipped_reviews == 0

    @pytest.mark.parametrize(
        "invalid_count",
        [float("nan"), float("inf"), float("-inf"), 1.5, "1", True, -1],
    )
    def test_skipped_reviews_must_be_a_non_negative_integer(self, invalid_count):
        with pytest.raises(
            ValueError, match="skipped_reviews must be a non-negative integer"
        ):
            CountryOutcome(
                country="us",
                pages=1,
                reviews_fetched=1,
                stopped_because="exhausted",
                elapsed=0.1,
                skipped_reviews=invalid_count,
            )

    def test_carries_error_for_failed_country(self):
        error = FetchError(country="de", message="denied", kind="auth", status=401)
        outcome = CountryOutcome(
            country="de",
            pages=0,
            reviews_fetched=0,
            stopped_because="error",
            elapsed=0.5,
            error=error,
        )
        assert outcome.error.kind == "auth"


class TestStructuredSerialization:
    def test_fetch_error_includes_retryability(self):
        error = FetchError(
            country="gb", message="try later", kind="rate_limited", status=429
        )

        assert error.to_dict() == {
            "country": "gb",
            "message": "try later",
            "kind": "rate_limited",
            "status": 429,
            "retryable": True,
        }

    def test_country_outcome_includes_counts_timing_stop_reason_and_error(self):
        error = FetchError(country="gb", message="broken", kind="server", status=503)
        outcome = CountryOutcome(
            country="gb",
            pages=2,
            reviews_fetched=37,
            stopped_because="error",
            elapsed=0.75,
            skipped_reviews=3,
            error=error,
        )

        assert outcome.to_dict() == {
            "country": "gb",
            "pages": 2,
            "reviews_fetched": 37,
            "skipped_reviews": 3,
            "stopped_because": "error",
            "elapsed": 0.75,
            "error": error.to_dict(),
        }

    def test_fetch_result_is_a_complete_json_safe_envelope(self):
        error = FetchError(country="gb", message="broken", kind="server", status=503)
        outcome = CountryOutcome(
            country="gb",
            pages=2,
            reviews_fetched=37,
            stopped_because="error",
            elapsed=0.75,
            skipped_reviews=3,
            error=error,
        )
        result = FetchResult(
            reviews=[make_review(raw={"provider": "payload"})], outcomes=[outcome]
        )

        serialised = result.to_dict()

        assert serialised == {
            "reviews": [make_review().to_dict()],
            "outcomes": [outcome.to_dict()],
            "errors": [error.to_dict()],
            "skipped_reviews": 3,
        }
        json.dumps(serialised, allow_nan=False)

    def test_fetch_result_aggregates_skips_while_preserving_outcomes(self):
        outcomes = [
            CountryOutcome(
                country="us",
                pages=1,
                reviews_fetched=10,
                stopped_because="exhausted",
                elapsed=0.1,
                skipped_reviews=2,
            ),
            CountryOutcome(
                country="gb",
                pages=1,
                reviews_fetched=8,
                stopped_because="exhausted",
                elapsed=0.2,
                skipped_reviews=1,
            ),
        ]
        result = FetchResult(outcomes=outcomes)

        assert result.skipped_reviews == 3
        assert result.to_dict()["skipped_reviews"] == 3
        assert result.to_dict()["outcomes"] == [
            outcome.to_dict() for outcome in outcomes
        ]

    def test_fetch_result_can_include_raw_review_payloads(self):
        result = FetchResult(reviews=[make_review(raw={"provider": "payload"})])

        assert result.to_dict(include_raw=True)["reviews"][0]["raw"] == {
            "provider": "payload"
        }

    def test_to_dicts_remains_review_row_compatibility(self):
        result = FetchResult(reviews=[make_review()], outcomes=[_outcome()])

        assert result.to_dicts() == result.to_dict()["reviews"]


class TestFetchResultOutcomes:
    def test_defaults_to_empty(self):
        assert FetchResult().outcomes == []

    def test_filter_preserves_outcomes(self):
        result = FetchResult(reviews=[], outcomes=[_outcome()])
        assert result.filter(ratings=[5]).outcomes == [_outcome()]

    def test_sort_preserves_outcomes(self):
        result = FetchResult(reviews=[], outcomes=[_outcome()])
        assert result.sort().outcomes == [_outcome()]

    def test_limit_preserves_outcomes(self):
        reviews = [make_review(id="1"), make_review(id="2")]
        result = FetchResult(reviews=reviews, outcomes=[_outcome()])
        limited = result.limit(1)
        assert len(limited.reviews) == 1
        assert limited.outcomes == [_outcome()]


class TestNoDerivedFieldsAreStored:
    """Three fields stored a value that was always computable from a neighbour.

    A stored derivation can disagree with its source. These cannot now.
    """

    def test_errors_is_derived_from_outcomes(self):
        err = FetchError(country="gb", message="boom", kind="rate_limited", status=429)
        result = FetchResult(
            reviews=[],
            outcomes=[
                CountryOutcome(
                    country="us",
                    pages=1,
                    reviews_fetched=5,
                    stopped_because="exhausted",
                    elapsed=0.5,
                ),
                CountryOutcome(
                    country="gb",
                    pages=1,
                    reviews_fetched=0,
                    stopped_because="error",
                    elapsed=0.5,
                    error=err,
                ),
            ],
        )

        assert result.errors == [err]

    def test_no_outcomes_means_no_errors(self):
        assert FetchResult(reviews=[]).errors == []

    def test_errors_cannot_be_set_independently_of_outcomes(self):
        """It was a constructor argument, so the two could contradict."""
        with pytest.raises(TypeError):
            FetchResult(reviews=[], errors=[1])  # type: ignore[call-arg]

    def test_errors_survives_a_transform(self):
        err = FetchError(country="gb", message="boom", kind="server", status=503)
        outcome = CountryOutcome(
            country="gb",
            pages=1,
            reviews_fetched=1,
            stopped_because="error",
            elapsed=0.1,
            error=err,
        )
        result = FetchResult(reviews=[make_review(rating=1)], outcomes=[outcome])

        assert result.filter(ratings=[5]).errors == [err]

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("rate_limited", True),
            ("server", True),
            ("transport", True),
            ("auth", False),
            ("not_found", False),
            ("parse", False),
        ],
    )
    def test_retryable_is_derived_from_kind(self, kind, expected):
        err = FetchError(country="us", message="x", kind=kind)

        assert err.retryable is expected

    def test_retryable_cannot_be_set(self):
        with pytest.raises(TypeError):
            FetchError(  # type: ignore[call-arg]
                country="us", message="x", kind="auth", retryable=True
            )


class TestOutcomeNamesWhatItCounts:
    """``CountryOutcome.reviews`` sat next to ``FetchResult.reviews`` meaning
    something else: one counts what the walk fetched, the other what survived
    filtering. A plain ``fetch(ratings=[...])`` made them differ.
    """

    def test_reviews_fetched_replaces_reviews(self):
        outcome = CountryOutcome(
            country="us",
            pages=3,
            reviews_fetched=30,
            stopped_because="exhausted",
            elapsed=0.5,
        )

        assert outcome.reviews_fetched == 30
        assert not hasattr(outcome, "reviews")


class TestElapsedIsRequired:
    """A default of 0.0 is a plausible-looking wrong timing.

    ``CountryCollector`` always knows the real duration (it starts a clock at
    construction), so the only thing the default enabled was a hand-built outcome
    claiming a fetch took no time at all.
    """

    def test_elapsed_must_be_supplied(self):
        with pytest.raises(TypeError):
            CountryOutcome(  # type: ignore[call-arg]
                country="us", pages=1, reviews_fetched=1, stopped_because="exhausted"
            )

    def test_a_real_walk_reports_a_real_duration(self):
        from app_reviews.core.paging import CountryCollector

        outcome = CountryCollector("us").outcome()

        assert outcome.elapsed > 0
