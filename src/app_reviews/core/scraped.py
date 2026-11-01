"""Coercers for untyped store payloads, and the placeholder for a missing field."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Any

UNKNOWN = "Unknown"
"""What an ``AppMetadata`` text field says when the store sent nothing usable."""


def scraped_text(value: Any) -> str | None:
    """Trimmed text from an untyped payload, or None if it is unusable.

    Both search clients fill ``AppMetadata`` fields declared ``str`` from values
    that arrive as JSON or as a scraped array, where the same key can be absent,
    explicitly ``null``, or a list. ``dict.get(key, default)`` only falls back
    when the key is *absent*, so a present ``null`` reached those fields before
    this existed.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return str(value)
    return (value.strip() or None) if isinstance(value, str) else None


def scraped_datetime(value: Any) -> datetime | None:
    """An ISO 8601 timestamp from an untyped payload, or None if it is unusable.

    Returns None rather than a default, unlike ``scraped_text`` and
    ``scraped_number``: an absent date has no stand-in that would not be mistaken
    for a real one downstream.

    Always tz-aware. A naive datetime raises ``TypeError`` the moment it is
    compared against an aware one, so a value iTunes sent without a zone is read
    as UTC, which is what it means by ``Z``.
    """
    if not isinstance(value, str) or not (text := value.strip()):
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


_ENGLISH_MONTHS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
_ENGLISH_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def english_month_number(abbreviation: str) -> int | None:
    """1 for ``"Jan"`` through 12 for ``"Dec"``, in any case; None otherwise.

    Store pages requested in English name months in English whatever the
    process locale is, while ``strptime``'s ``%b`` follows ``LC_TIME``. Reading
    the name against this fixed table and handing ``strptime`` the number keeps
    parsing independent of the locale the embedding application runs under.
    """
    lowered = abbreviation.lower()
    if lowered not in _ENGLISH_MONTHS:
        return None
    return _ENGLISH_MONTHS.index(lowered) + 1


def is_english_weekday(abbreviation: str) -> bool:
    """Whether this is ``"Mon"`` through ``"Sun"``, in any case."""
    return abbreviation.lower() in _ENGLISH_WEEKDAYS


def scraped_number(value: Any, default: float) -> float:
    """A float from an untyped payload, or ``default`` if it is unusable.

    A rating that arrives as ``"four-point-five"`` costs the field, not the call:
    ``float()`` on it raises, and neither search method has a way to report one
    bad field on an otherwise usable result.
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def scraped_rating(value: Any) -> float:
    """A store rating in its valid 0..5 domain, otherwise the safe fallback."""
    parsed = scraped_number(value, -1.0)
    return parsed if 0 <= parsed <= 5 else 0.0


def scraped_rating_count(value: Any) -> int:
    """A whole, non-negative rating count, otherwise the safe fallback."""
    parsed = scraped_number(value, -1.0)
    return int(parsed) if parsed >= 0 and parsed.is_integer() else 0
