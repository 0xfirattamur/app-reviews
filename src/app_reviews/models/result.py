"""Fetch result and related models."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from math import isfinite
from typing import Any

from app_reviews.models.review import Review
from app_reviews.models.types import RETRYABLE_KINDS, ErrorKind, Sort, StopReason


def _validate_non_negative_int(value: object, field_name: str) -> None:
    """Reject values that cannot be a JSON-safe count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class FetchError:
    """A per-country fetch failure.

    Branch retry policy on ``kind``; ``retryable`` is only this package's opinion
    about the same fact.
    """

    country: str | None
    message: str
    kind: ErrorKind
    status: int | None = None

    @property
    def retryable(self) -> bool:
        """Whether this kind of failure is worth retrying."""
        return self.kind in RETRYABLE_KINDS

    def to_dict(self) -> dict[str, Any]:
        """Return the complete JSON-safe error contract."""
        return {
            "country": self.country,
            "message": self.message,
            "kind": self.kind,
            "status": self.status,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class CountryOutcome:
    """What one country's fetch actually did.

    ``stopped_because`` distinguishes "there is no more data" (``exhausted``)
    from "we stopped asking" (``limit``, ``since``).

    ``reviews_fetched`` is what this country's walk pulled off the wire, before
    filtering, so it can exceed ``len(result.reviews)``.
    """

    country: str | None
    pages: int
    reviews_fetched: int
    stopped_because: StopReason
    elapsed: float
    error: FetchError | None = None
    skipped_reviews: int = 0

    def __post_init__(self) -> None:
        if not isfinite(self.elapsed) or self.elapsed < 0:
            raise ValueError("elapsed must be finite and non-negative")
        _validate_non_negative_int(self.skipped_reviews, "skipped_reviews")

    def to_dict(self) -> dict[str, Any]:
        """Return the complete JSON-safe per-country outcome."""
        return {
            "country": self.country,
            "pages": self.pages,
            "reviews_fetched": self.reviews_fetched,
            "skipped_reviews": self.skipped_reviews,
            "stopped_because": self.stopped_because,
            "elapsed": self.elapsed,
            "error": self.error.to_dict() if self.error is not None else None,
        }


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Structured result container for a fetch operation."""

    reviews: list[Review] = field(default_factory=list)
    outcomes: list[CountryOutcome] = field(default_factory=list)

    @property
    def errors(self) -> list[FetchError]:
        """Every failure, in country order.

        Read-only, so a filtered or truncated result cannot lose or contradict
        them: there is one copy, on the outcomes.
        """
        return [o.error for o in self.outcomes if o.error is not None]

    @property
    def skipped_reviews(self) -> int:
        """Total malformed or unusable review rows reported by all outcomes."""
        return sum(outcome.skipped_reviews for outcome in self.outcomes)

    def __iter__(self) -> Iterator[Review]:
        return iter(self.reviews)

    def __len__(self) -> int:
        return len(self.reviews)

    def __bool__(self) -> bool:
        return bool(self.reviews)

    def filter(
        self,
        *,
        ratings: list[int] | None = None,
        since: date | datetime | None = None,
        until: date | datetime | None = None,
    ) -> FetchResult:
        """Return a new FetchResult with filtered reviews (non-destructive)."""
        filtered = list(self.reviews)
        if ratings is not None:
            rating_set = set(ratings)
            filtered = [r for r in filtered if r.rating in rating_set]
        if since is not None:
            since_dt = to_aware_datetime(since)
            filtered = [r for r in filtered if r.dated_at >= since_dt]
        if until is not None:
            until_dt = to_aware_datetime(until, end_of_day=True)
            filtered = [r for r in filtered if r.dated_at <= until_dt]
        return FetchResult(reviews=filtered, outcomes=self.outcomes)

    def sort(self, order: Sort = Sort.NEWEST) -> FetchResult:
        """Return a new FetchResult with sorted reviews (non-destructive)."""
        key, reverse = self._sort_key(order)
        return FetchResult(
            reviews=sorted(self.reviews, key=key, reverse=reverse),
            outcomes=self.outcomes,
        )

    def _sort_key(self, order: Sort) -> tuple[Callable[[Review], Any], bool]:
        """The key function and direction for one ordering.

        Date orderings read ``dated_at`` rather than either timestamp directly,
        so they compare whichever field the source actually ordered by. ``order``
        is coerced so an equivalent plain string still works (``Sort`` is a
        ``StrEnum``), and an unusable one says so instead of raising ``KeyError``.
        """
        coerced = Sort(order)
        if coerced is Sort.RATING:
            return (lambda review: review.rating), True
        return (lambda review: review.dated_at), coerced is Sort.NEWEST

    def limit(self, n: int | None) -> FetchResult:
        """Return a new FetchResult truncated to n reviews. No-op if n is None.

        A non-positive ``n`` means none. Guarded explicitly because ``n >= len``
        is never true for a negative number, so ``reviews[:n]`` used to slice
        from the tail and quietly drop the newest instead.
        """
        if n is None or n >= len(self.reviews):
            return self
        if n <= 0:
            return FetchResult(reviews=[], outcomes=self.outcomes)
        return FetchResult(reviews=self.reviews[:n], outcomes=self.outcomes)

    def to_dicts(self, *, include_raw: bool = False) -> list[dict[str, Any]]:
        """JSON-serialisable plain dicts, one per review.

        Delegates to ``Review.to_dict``, which is the package's only
        Review-to-dict conversion. See it for why ``raw`` is opt-in.
        """
        return [r.to_dict(include_raw=include_raw) for r in self.reviews]

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        """Return a complete JSON-safe result envelope.

        ``to_dicts`` remains the compatibility API for callers that only need
        review rows. This envelope also preserves diagnostics so an empty
        success cannot be confused with a provider failure.
        """
        return {
            "reviews": self.to_dicts(include_raw=include_raw),
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "errors": [error.to_dict() for error in self.errors],
            "skipped_reviews": self.skipped_reviews,
        }


def to_aware_datetime(d: date | datetime, *, end_of_day: bool = False) -> datetime:
    """Convert a date or naive datetime to a UTC-aware datetime.

    The one implementation, so the page walk's early stop and ``filter``'s
    boundary check cannot disagree about what ``since``/``until`` means. A date
    used as an inclusive upper bound resolves to the final microsecond of that
    UTC day; datetime inputs retain their precise instant.
    """
    if isinstance(d, datetime):
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    return datetime.combine(d, time.max if end_of_day else time.min, tzinfo=UTC)
