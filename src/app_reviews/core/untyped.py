"""One policy for reading a store's untyped review payload.

A review page is a list of entries the store built and we did not. Reading one
field wrong must cost that review, never the page, and never an exception out of
``fetch_page``. The strict readers here raise ``UnreadablePayload``, a provider
catches ``UNREADABLE_PAYLOAD`` around one entry, and ``page_from_entries`` turns
the ones that failed into ``skipped_reviews``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app_reviews.models.page import PageResult
from app_reviews.models.review import Review


class UnreadablePayload(Exception):
    """A payload field that is not the type its store documents.

    The message names the field and the types only. The offending value is left
    out on purpose: it can be large, and in a review entry it can be something
    the reviewer wrote.
    """

    def __init__(self, field: str, expected: str, value: object) -> None:
        self.field: str = field
        self.expected: str = expected
        self.got: str = type(value).__name__
        super().__init__(f"{field} is {self.got}, expected {expected}")


UNREADABLE_PAYLOAD = (
    UnreadablePayload,
    AttributeError,
    IndexError,
    KeyError,
    OSError,
    OverflowError,
    TypeError,
    ValueError,
)
"""Everything that reading an untyped payload can raise.

``OverflowError`` and ``OSError`` are in here because ``datetime.fromtimestamp``
raises them for a seconds value outside the platform's range, and neither is a
``ValueError``, so one absurd timestamp escaped all the way out of
``fetch_page``, which ``iter_pages`` promises never happens.
"""


def expect_str(value: Any, field: str) -> str:
    """``value`` if it is a string; anything else is unreadable, never coerced."""
    if not isinstance(value, str):
        raise UnreadablePayload(field, "a string", value)
    return value


def expect_int(value: Any, field: str) -> int:
    """``value`` if it is an integer. ``True`` is not one, and a float is not coerced.

    Coercing would truncate a corrupt ``4.9`` into a plausible rating.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnreadablePayload(field, "an integer", value)
    return value


def optional_str(value: Any, field: str) -> str | None:
    """None when the field is absent or null; otherwise as ``expect_str``."""
    if value is None:
        return None
    return expect_str(value, field)


def optional_count(value: Any) -> int | None:
    """A non-negative integer, or None for anything else. ``True`` is not a count.

    Lenient where the readers above are strict: it is for enrichments, whose
    loss must not cost the review, so an unusable one is dropped, not raised.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def log_safe_id(entry: Any, id_slot: str | int) -> str | int | None:
    """The review id in ``entry``, or None; fit for a warning that names the entry.

    Logging the entry itself put the reviewer's name, their text and the URL of
    their photo into a log line. ``id_slot`` is the key of an object entry or the
    index of an array entry. A hostile id that is neither a string nor an integer
    is dropped too, since it could be anything.
    """
    if isinstance(id_slot, str) and isinstance(entry, dict):
        entry_id = entry.get(id_slot)
    elif isinstance(id_slot, int) and isinstance(entry, list) and id_slot < len(entry):
        entry_id = entry[id_slot]
    else:
        return None
    if isinstance(entry_id, bool) or not isinstance(entry_id, str | int):
        return None
    return entry_id


def page_from_entries(
    entries: list[Any],
    parse_entry: Callable[[Any], Review | None],
    next_cursor: str | None,
) -> PageResult:
    """The page's reviews; an entry ``parse_entry`` returns None for is skipped.

    A malformed envelope fails the whole page, but one bad entry must not:
    failing the page discards ``next_cursor`` and silently ends the walk, so
    ``skipped_reviews`` counts the entries that were dropped instead.
    """
    parsed = (parse_entry(entry) for entry in entries)
    reviews = [review for review in parsed if review is not None]
    return PageResult(
        reviews=reviews,
        next_cursor=next_cursor,
        skipped_reviews=len(entries) - len(reviews),
    )
