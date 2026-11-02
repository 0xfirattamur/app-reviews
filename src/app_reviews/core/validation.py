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


_DOT_SEGMENTS = frozenset({".", ".."})


def require_path_segment(value: str, name: str) -> str:
    """Reject a value that would act as a dot segment inside a URL path.

    ``quote`` leaves ``.`` alone, so ``..`` survives escaping and the client
    normalises it away, retargeting the request (and its credential) at another
    endpoint. Returns ``value`` so a caller can validate where it uses it.
    """
    if value in _DOT_SEGMENTS:
        raise ValueError(f"{name} must not be {value!r}: it would change the path")
    return value
