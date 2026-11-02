"""Tests for the coercers that read untyped store payloads."""

from datetime import UTC, datetime

import pytest

from app_reviews.core.scraped import (
    UNKNOWN,
    english_month_number,
    is_english_weekday,
    scraped_datetime,
    scraped_number,
    scraped_rating,
    scraped_rating_count,
    scraped_text,
)


class TestScrapedNumber:
    @pytest.mark.parametrize(
        "value", [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity"]
    )
    def test_non_finite_values_fall_back_to_the_default(self, value):
        assert scraped_number(value, 0.0) == 0.0

    def test_an_integer_too_large_for_a_float_falls_back_to_the_default(self):
        assert scraped_number(10**400, 7.0) == 7.0


class TestScrapedText:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (" a ", "a"),
            ("  ", None),
            (None, None),
            (True, None),
            (1.5, "1.5"),
            (["x"], None),
        ],
    )
    def test_text_is_trimmed_or_absent(self, value, expected):
        assert scraped_text(value) == expected

    def test_the_placeholder_is_the_word_unknown(self):
        assert UNKNOWN == "Unknown"


class TestScrapedDatetime:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2024-01-02T03:04:05Z", datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)),
            ("2024-01-02T03:04:05", datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)),
            ("not a date", None),
            (" ", None),
            (5, None),
        ],
    )
    def test_always_aware_or_none(self, value, expected):
        assert scraped_datetime(value) == expected


class TestScrapedRatings:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(4.5, 4.5), ("3", 3.0), (6, 0.0), (-1, 0.0), (None, 0.0), ("x", 0.0)],
    )
    def test_rating_stays_within_zero_to_five(self, value, expected):
        assert scraped_rating(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(12, 12), ("7", 7), (1.5, 0), (-3, 0), (None, 0), (True, 0)],
    )
    def test_rating_count_is_whole_and_non_negative(self, value, expected):
        assert scraped_rating_count(value) == expected


class TestEnglishDates:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [("Jan", 1), ("dec", 12), ("MAY", 5), ("Foo", None), ("", None)],
    )
    def test_month_number_ignores_case_and_locale(self, name, expected):
        assert english_month_number(name) == expected

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("Mon", True), ("sun", True), ("Funday", False), ("", False)],
    )
    def test_weekday_ignores_case(self, name, expected):
        assert is_english_weekday(name) is expected
