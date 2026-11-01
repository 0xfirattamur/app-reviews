"""Tests for the one policy that reads untyped review payloads."""

from datetime import datetime

import pytest

from app_reviews.core.untyped import (
    UNREADABLE_PAYLOAD,
    UnreadablePayload,
    expect_int,
    expect_str,
    log_safe_id,
    optional_count,
    optional_str,
    page_from_entries,
)
from tests.app_reviews.factories import make_review


class TestUnreadablePayload:
    def test_carries_the_field_and_the_types_but_not_the_value(self):
        error = UnreadablePayload("rating", "an integer", "secret reviewer text")

        assert (error.field, error.expected, error.got) == (
            "rating",
            "an integer",
            "str",
        )
        assert str(error) == "rating is str, expected an integer"
        assert "secret" not in str(error)

    def test_is_caught_by_the_unreadable_payload_tuple(self):
        with pytest.raises(UNREADABLE_PAYLOAD):
            expect_str(1, "field")

    @pytest.mark.parametrize(
        "error",
        [
            AttributeError,
            IndexError,
            KeyError,
            OSError,
            OverflowError,
            TypeError,
            ValueError,
        ],
    )
    def test_the_tuple_covers_what_reading_an_untyped_payload_raises(self, error):
        with pytest.raises(UNREADABLE_PAYLOAD):
            raise error("x")

    def test_an_absurd_timestamp_is_covered(self):
        with pytest.raises(UNREADABLE_PAYLOAD):
            datetime.fromtimestamp(10**18)


class TestExpectStr:
    def test_returns_a_string_as_is(self):
        assert expect_str(" a ", "field") == " a "

    @pytest.mark.parametrize("value", [None, 1, 1.5, True, ["a"], {"a": 1}])
    def test_rejects_anything_else(self, value):
        with pytest.raises(UnreadablePayload, match=r"field is .*, expected a string"):
            expect_str(value, "field")


class TestExpectInt:
    def test_returns_an_integer_as_is(self):
        assert expect_int(4, "field") == 4

    @pytest.mark.parametrize("value", [None, True, False, 4.0, "4", [4]])
    def test_rejects_anything_else(self, value):
        with pytest.raises(UnreadablePayload, match="expected an integer"):
            expect_int(value, "field")


class TestOptionalStr:
    @pytest.mark.parametrize(
        ("value", "expected"), [(None, None), ("a", "a"), ("", "")]
    )
    def test_absent_is_none_and_a_string_is_kept(self, value, expected):
        assert optional_str(value, "field") == expected

    def test_a_present_non_string_is_unreadable(self):
        with pytest.raises(UnreadablePayload, match="field is int"):
            optional_str(3, "field")


class TestOptionalCount:
    @pytest.mark.parametrize(("value", "expected"), [(0, 0), (7, 7)])
    def test_keeps_a_non_negative_integer(self, value, expected):
        assert optional_count(value) == expected

    @pytest.mark.parametrize("value", [None, -1, True, 1.0, "1", []])
    def test_drops_anything_else_without_raising(self, value):
        assert optional_count(value) is None


class TestLogSafeId:
    def test_reads_an_object_entry_by_key(self):
        assert log_safe_id({"id": "r1", "body": "private"}, "id") == "r1"

    def test_reads_an_array_entry_by_index(self):
        assert log_safe_id(["r1", ["Alice"]], 0) == "r1"

    @pytest.mark.parametrize(
        ("entry", "id_slot"),
        [
            ("not an entry", "id"),
            ({}, "id"),
            ([], 0),
            ({"id": {"nested": "private"}}, "id"),
            ({"id": True}, "id"),
            (["r1"], "id"),
            ({"id": "r1"}, 0),
        ],
    )
    def test_is_none_when_there_is_no_plain_id(self, entry, id_slot):
        assert log_safe_id(entry, id_slot) is None


class TestPageFromEntries:
    def test_keeps_parsed_reviews_and_counts_the_dropped_ones(self):
        kept = make_review(id="kept")

        def parse_entry(entry):
            return kept if entry == "good" else None

        page = page_from_entries(["good", "bad", "good"], parse_entry, "next")

        assert page.reviews == [kept, kept]
        assert page.skipped_reviews == 1
        assert page.next_cursor == "next"
        assert page.error is None

    def test_no_entries_is_an_empty_page_without_skips(self):
        page = page_from_entries([], lambda entry: None, None)

        assert page.reviews == []
        assert page.skipped_reviews == 0
        assert page.next_cursor is None
