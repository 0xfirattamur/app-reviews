"""Canonical normalized review model."""

from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any

from app_reviews.models.types import Source, Store


@dataclass(frozen=True, slots=True)
class Review:
    """Canonical normalized review from any provider.

    ``id`` is the raw store identifier, unique within a ``(store, source)``
    pair and **not** comparable across sources. Key deduplication on
    ``(store, source, id)``.

    ``country`` is the storefront that was queried, not the reviewer's
    location. ``None`` means the source does not report it.

    ``title`` is ``None`` where the source has no title concept; Google Play
    reviews have no titles.

    No source reports both timestamps, so exactly one of ``created_at`` and
    ``updated_at`` is set and it is the field that source orders by. Read
    ``dated_at`` when you want the one that is there.
    """

    store: Store
    app_id: str
    country: str | None
    rating: int
    title: str | None
    body: str
    author_name: str
    source: Source
    id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    app_version: str | None = None
    language: str | None = None
    fetched_at: datetime | None = None
    raw: dict[str, Any] | list[Any] | None = None
    """The provider's own payload, exactly as it arrived.

    A list as well as an object because the two stores disagree: the iTunes and
    Connect APIs send JSON objects, while Play's endpoints send positional
    arrays. Wrapping the arrays would make ``raw`` mean "what the source sent,
    unless the source is Play", which is worse than one wider type.
    """

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("id must not be empty")
        if not 1 <= self.rating <= 5:
            raise ValueError(f"rating must be 1-5, got {self.rating}")
        if (self.created_at is None) == (self.updated_at is None):
            raise ValueError("exactly one of created_at or updated_at is required")
        for name in ("created_at", "updated_at", "fetched_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                object.__setattr__(self, name, value.replace(tzinfo=UTC))

    @property
    def dated_at(self) -> datetime:
        """Whichever timestamp this review's source reports.

        Sorting, ``since``/``until`` and the page-walk early stop all read this,
        so they compare the field the source actually ordered by. Derived rather
        than stored: there is nothing to keep in sync.

        ``__post_init__`` guarantees exactly one of the pair is set, but that is
        a runtime fact a type checker cannot carry to here. Re-checking states it
        in a way both mypy and a reader can see, and turns a violated invariant
        into a named error rather than a ``None`` that fails somewhere downstream.
        """
        dated = self.updated_at or self.created_at
        if dated is None:  # pragma: no cover - __post_init__ rules this out
            raise ValueError("a review needs created_at or updated_at")
        return dated

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        """A JSON-serialisable plain dict of this review.

        Two decisions live here because this class owns the fields they concern.

        ``raw`` is excluded unless asked for: it is the provider's own payload,
        kept so a fetch can be reprocessed later, and it routinely dwarfs the
        review around it. When included it is passed through by reference rather
        than copied: ``dataclasses.asdict`` recursively copied it even on the
        path that immediately discarded it, which cost ~23x the whole conversion.

        Timestamps become ISO 8601 strings via ``isoformat()``. Serialising them
        with ``str()`` yields a space where ISO wants a ``T``, which strict
        parsers downstream reject.
        """
        out: dict[str, Any] = {}
        for name in _FIELD_NAMES:
            if name == "raw" and not include_raw:
                continue
            value = getattr(self, name)
            out[name] = value.isoformat() if isinstance(value, datetime) else value
        return out


_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in fields(Review))
"""Resolved once at import rather than per conversion."""
