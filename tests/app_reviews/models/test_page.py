"""Tests for PageResult."""

import json

import pytest

from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError
from tests.app_reviews.factories import make_review


class TestPageResult:
    def test_defaults(self):
        page = PageResult()
        assert page.reviews == []
        assert page.skipped_reviews == 0
        assert page.next_cursor is None
        assert page.error is None
        assert page.stopped_because is None

    @pytest.mark.parametrize(
        "invalid_count",
        [float("nan"), float("inf"), float("-inf"), 1.5, "1", True, -1],
    )
    def test_skipped_reviews_must_be_a_non_negative_integer(self, invalid_count):
        with pytest.raises(
            ValueError, match="skipped_reviews must be a non-negative integer"
        ):
            PageResult(skipped_reviews=invalid_count)

    def test_carries_stop_reason(self):
        page = PageResult(stopped_because="since")
        assert page.stopped_because == "since"

    def test_carries_error(self):
        page = PageResult(
            error=FetchError(country="us", message="boom", kind="server", status=503)
        )
        assert page.error.kind == "server"

    def test_to_dict_is_a_complete_json_safe_envelope(self):
        error = FetchError(country="us", message="boom", kind="server", status=503)
        page = PageResult(
            reviews=[make_review(raw={"provider": "payload"})],
            next_cursor="next-page",
            error=error,
            stopped_because="error",
        )

        serialised = page.to_dict()

        assert serialised == {
            "reviews": [make_review().to_dict()],
            "skipped_reviews": 0,
            "next_cursor": "next-page",
            "error": error.to_dict(),
            "stopped_because": "error",
        }
        json.dumps(serialised, allow_nan=False)

    def test_to_dict_can_include_raw_review_payloads(self):
        page = PageResult(reviews=[make_review(raw={"provider": "payload"})])

        assert page.to_dict(include_raw=True)["reviews"][0]["raw"] == {
            "provider": "payload"
        }
