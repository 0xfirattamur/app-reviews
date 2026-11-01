"""Small validators for numeric inputs the caller supplies.

Each raises ``ValueError`` naming the field. They exist because ``bool`` is an
``int`` and ``nan`` compares false against everything, so a plain ``< 0`` check
lets ``True`` and ``float("nan")`` through.
"""

from __future__ import annotations

from math import isfinite

MIN_HTTP_STATUS = 100
MAX_HTTP_STATUS = 599


def require_count(value: object, field_name: str) -> None:
    """Reject anything that is not a non-negative ``int`` (``bool`` included)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def require_finite_non_negative(value: object, field_name: str) -> None:
    """Reject anything that is not a finite number >= 0 (``bool`` included)."""
    if not _is_finite_number(value) or value < 0:  # type: ignore[operator]
        raise ValueError(f"{field_name} must be a finite number >= 0")


def require_finite_positive(value: object, field_name: str) -> None:
    """Reject anything that is not a finite number > 0 (``bool`` included)."""
    if not _is_finite_number(value) or value <= 0:  # type: ignore[operator]
        raise ValueError(f"{field_name} must be a finite number > 0")


def require_http_statuses(values: object, field_name: str) -> None:
    """Reject any entry that is not an ``int`` from 100 to 599 (``bool`` excluded)."""
    for status in values:  # type: ignore[attr-defined]
        if isinstance(status, bool) or not isinstance(status, int):
            raise ValueError(f"{field_name} entries must be HTTP status codes")
        if not MIN_HTTP_STATUS <= status <= MAX_HTTP_STATUS:
            raise ValueError(
                f"{field_name} entries must be HTTP status codes "
                f"({MIN_HTTP_STATUS}..{MAX_HTTP_STATUS})"
            )


def _is_finite_number(value: object) -> bool:
    """Whether ``value`` is an ``int`` or ``float`` (not ``bool``) and finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return isfinite(value)
