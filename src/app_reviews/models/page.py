"""Single-page provider result."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app_reviews.models._json import fields_to_dict
from app_reviews.models._validate import require_count
from app_reviews.models.result import FetchError
from app_reviews.models.review import Review
from app_reviews.models.types import FeedFormat, StopReason

_REMAINING_KEY_ORDER = (
    "skipped_reviews",
    "next_cursor",
    "error",
    "stopped_because",
    "feed_format",
    "xml_fallback_error",
)
"""The order ``to_dict`` has always emitted after ``reviews``; consumers rely on it."""


@dataclass(frozen=True, slots=True)
class PageResult:
    """Result of a single provider page request.

    ``next_cursor`` is opaque and provider-specific; persist it verbatim to
    resume a walk later. ``None`` means there are no more pages.

    ``stopped_because`` is set only on the last page of a walk.

    ``feed_format`` says which App Store RSS feed answered: ``"json"``, or
    ``"xml"`` when the JSON feed came back empty or unreadable and its XML twin
    had the entries. None for other sources, and for a page that failed.

    ``xml_fallback_error`` is set when the JSON feed answered empty and the XML
    feed asked to confirm it could not be read. The page still counts as the
    JSON answer; this only says the emptiness went unconfirmed.
    """

    reviews: list[Review] = field(default_factory=list)
    next_cursor: str | None = None
    error: FetchError | None = None
    stopped_because: StopReason | None = None
    skipped_reviews: int = 0
    feed_format: FeedFormat | None = None
    xml_fallback_error: FetchError | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        require_count(self.skipped_reviews, "skipped_reviews")

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        """Return the complete JSON-safe page envelope."""
        return {
            "reviews": [
                review.to_dict(include_raw=include_raw) for review in self.reviews
            ],
            **fields_to_dict(
                self, exclude=("reviews",), key_order=_REMAINING_KEY_ORDER
            ),
        }
