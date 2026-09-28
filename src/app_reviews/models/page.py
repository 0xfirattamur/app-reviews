"""Single-page provider result."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app_reviews.models.result import FetchError, _validate_non_negative_int
from app_reviews.models.review import Review
from app_reviews.models.types import FeedFormat, StopReason


@dataclass(frozen=True, slots=True)
class PageResult:
    """Result of a single provider page request.

    ``next_cursor`` is opaque and provider-specific; persist it verbatim to
    resume a walk later. ``None`` means there are no more pages.

    ``stopped_because`` is set only on the last page of a walk.

    ``feed_format`` says which App Store RSS feed answered: ``"json"``, or
    ``"xml"`` when the JSON feed came back empty or unreadable and its XML twin
    had the entries. None for other sources, and for a page that failed.
    """

    reviews: list[Review] = field(default_factory=list)
    next_cursor: str | None = None
    error: FetchError | None = None
    stopped_because: StopReason | None = None
    skipped_reviews: int = 0
    feed_format: FeedFormat | None = None

    def __post_init__(self) -> None:
        _validate_non_negative_int(self.skipped_reviews, "skipped_reviews")

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        """Return the complete JSON-safe page envelope."""
        return {
            "reviews": [
                review.to_dict(include_raw=include_raw) for review in self.reviews
            ],
            "skipped_reviews": self.skipped_reviews,
            "next_cursor": self.next_cursor,
            "error": self.error.to_dict() if self.error is not None else None,
            "stopped_because": self.stopped_because,
            "feed_format": self.feed_format,
        }
