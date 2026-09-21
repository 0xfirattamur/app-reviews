"""Tests for AppMetadata icon_url field."""

import json
from datetime import UTC, datetime

import pytest

from app_reviews.models.metadata import AppMetadata


def _metadata(**overrides):
    values = {
        "app_id": "123",
        "store": "appstore",
        "name": "Test",
        "developer": "Dev",
        "category": "Utils",
        "price": "Free",
        "version": "1.0",
        "rating": 4.5,
        "rating_count": 100,
        "url": "https://example.com",
    }
    return AppMetadata(**(values | overrides))


class TestAppMetadataIconUrl:
    def test_icon_url_defaults_to_none(self):
        meta = AppMetadata(
            app_id="123",
            store="appstore",
            name="Test",
            developer="Dev",
            category="Utils",
            price="Free",
            version="1.0",
            rating=4.5,
            rating_count=100,
            url="https://example.com",
        )
        assert meta.icon_url is None

    def test_icon_url_can_be_set(self):
        meta = AppMetadata(
            app_id="123",
            store="appstore",
            name="Test",
            developer="Dev",
            category="Utils",
            price="Free",
            version="1.0",
            rating=4.5,
            rating_count=100,
            url="https://example.com",
            icon_url="https://example.com/icon.png",
        )
        assert meta.icon_url == "https://example.com/icon.png"


class TestAppMetadataToDict:
    def test_every_field_survives_and_dates_are_iso_8601(self):
        metadata = _metadata(
            icon_url="https://example.com/icon.png",
            current_version_release_date=datetime(2025, 1, 2, 12, 30, tzinfo=UTC),
            first_release_date=datetime(2020, 5, 6, tzinfo=UTC),
        )

        serialised = metadata.to_dict()

        assert serialised == {
            "app_id": "123",
            "store": "appstore",
            "name": "Test",
            "developer": "Dev",
            "category": "Utils",
            "price": "Free",
            "version": "1.0",
            "rating": 4.5,
            "rating_count": 100,
            "url": "https://example.com",
            "icon_url": "https://example.com/icon.png",
            "current_version_release_date": "2025-01-02T12:30:00+00:00",
            "first_release_date": "2020-05-06T00:00:00+00:00",
        }
        json.dumps(serialised, allow_nan=False)

    @pytest.mark.parametrize("rating", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_ratings_are_rejected(self, rating):
        with pytest.raises(ValueError, match="rating must be finite"):
            _metadata(rating=rating)

    @pytest.mark.parametrize(
        "rating_count", [float("nan"), float("inf"), float("-inf")]
    )
    def test_non_finite_rating_counts_are_rejected(self, rating_count):
        with pytest.raises(ValueError, match="rating_count must be finite"):
            _metadata(rating_count=rating_count)
