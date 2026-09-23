"""Tests for Review model validation."""

from datetime import UTC, datetime

import pytest

from app_reviews.models.review import Review


def _make_review(**overrides):
    defaults = {
        "store": "appstore",
        "app_id": "12345",
        "country": "us",
        "rating": 4,
        "title": "Great",
        "body": "Love it",
        "author_name": "Tester",
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        "source": "appstore_scraper",
        "id": "review-1",
    }
    return Review(**(defaults | overrides))


class TestReviewRatingValidation:
    def test_valid_rating_1(self):
        review = _make_review(rating=1)
        assert review.rating == 1

    def test_valid_rating_5(self):
        review = _make_review(rating=5)
        assert review.rating == 5

    def test_rating_0_raises(self):
        with pytest.raises(ValueError, match="rating must be 1-5"):
            _make_review(rating=0)

    def test_rating_6_raises(self):
        with pytest.raises(ValueError, match="rating must be 1-5"):
            _make_review(rating=6)

    def test_negative_rating_raises(self):
        with pytest.raises(ValueError, match="rating must be 1-5"):
            _make_review(rating=-1)

    @pytest.mark.parametrize("rating", [True, False, 1.5, "5"])
    def test_rating_must_be_a_real_integer(self, rating):
        with pytest.raises(ValueError, match="rating must be an integer"):
            _make_review(rating=rating)


class TestReviewIdentityAndTimestampValidation:
    def test_id_is_required(self):
        with pytest.raises(TypeError):
            Review(  # type: ignore[call-arg]
                store="appstore",
                app_id="12345",
                country="us",
                rating=4,
                title="Great",
                body="Love it",
                author_name="Tester",
                source="appstore_scraper",
                created_at=datetime(2024, 1, 1, tzinfo=UTC),
            )

    @pytest.mark.parametrize("empty_id", ["", "   "])
    def test_id_must_not_be_empty(self, empty_id):
        with pytest.raises(ValueError, match="id must not be empty"):
            _make_review(id=empty_id)

    def test_exactly_one_source_timestamp_is_required(self):
        with pytest.raises(ValueError, match="exactly one"):
            _make_review(created_at=None, updated_at=None)

        with pytest.raises(ValueError, match="exactly one"):
            _make_review(updated_at=datetime(2024, 1, 2, tzinfo=UTC))

    def test_naive_timestamps_are_normalised_to_utc(self):
        created = datetime(2024, 1, 1, 12, 30)
        fetched = datetime(2024, 1, 2, 9, 45)

        review = _make_review(created_at=created, fetched_at=fetched)

        assert review.created_at == created.replace(tzinfo=UTC)
        assert review.fetched_at == fetched.replace(tzinfo=UTC)


class TestNullableFields:
    def test_country_accepts_none(self):
        review = _make_review(country=None)
        assert review.country is None

    def test_title_accepts_none(self):
        review = _make_review(title=None)
        assert review.title is None

    def test_country_still_accepts_a_code(self):
        assert _make_review(country="gb").country == "gb"


class TestToDict:
    """``Review`` owns the knowledge that ``raw`` is payload and a timestamp is
    not JSON-safe, because ``Review`` owns those fields.

    That knowledge used to live in ``app_reviews.export.review_to_dict`` while
    ``FetchResult.to_dicts`` had its own ``asdict`` call that knew neither, so
    there were two conversions and only one of them produced serialisable output.
    """

    def test_excludes_raw_by_default(self):
        review = _make_review(raw={"entry": ["payload"]})

        assert "raw" not in review.to_dict()

    def test_includes_raw_when_asked(self):
        review = _make_review(raw={"entry": ["payload"]})

        assert review.to_dict(include_raw=True)["raw"] == {"entry": ["payload"]}

    def test_raw_is_passed_through_not_copied(self):
        """A deep copy of the payload only to serialise it is waste; asdict did
        exactly that, and then the exporter threw the copy away."""
        payload = {"entry": ["payload"]}
        review = _make_review(raw=payload)

        assert review.to_dict(include_raw=True)["raw"] is payload

    def test_every_other_field_survives(self):
        import dataclasses

        expected = {f.name for f in dataclasses.fields(Review)} - {"raw"}

        assert set(_make_review().to_dict()) == expected

    def test_timestamps_are_iso_8601(self):
        """``default=str`` rendered '2024-03-15 10:00:00+00:00': a space where
        ISO 8601 wants a T, which trips strict parsers downstream."""
        review = _make_review(created_at=datetime(2024, 3, 15, 10, 0, tzinfo=UTC))

        assert review.to_dict()["created_at"] == "2024-03-15T10:00:00+00:00"

    def test_absent_timestamps_stay_none(self):
        review = _make_review(updated_at=None, fetched_at=None)
        d = review.to_dict()

        assert d["updated_at"] is None
        assert d["fetched_at"] is None

    def test_the_result_is_json_serialisable(self):
        """The whole point: json.dumps(to_dicts()) used to raise TypeError."""
        import json

        json.dumps(_make_review().to_dict())
