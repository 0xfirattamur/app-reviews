"""Shared validation for public request bounds."""

from __future__ import annotations


def require_non_negative(value: int | None, name: str) -> None:
    """Reject a negative optional request bound."""
    if value is not None and value < 0:
        raise ValueError(f"{name} must be >= 0")


def require_positive(value: int | None, name: str) -> None:
    """Reject a non-positive optional concurrency-style bound."""
    if value is not None and value <= 0:
        raise ValueError(f"{name} must be > 0")
