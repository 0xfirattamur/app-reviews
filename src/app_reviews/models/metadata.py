"""App metadata model."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from math import isfinite
from typing import Any

from app_reviews.models.types import Store


@dataclass(frozen=True, slots=True)
class AppMetadata:
    """App metadata from a store lookup.

    Text and number fields are non-optional, so a store that does not publish one
    reports a stated placeholder rather than ``None``. Two are worth knowing about.
    A Play *search* hit carries no rating count, so ``rating_count`` is ``0`` unless
    it came from ``lookup()`` or was the one featured hit. And ``version`` on Play
    is ``"Varies with device"`` for an app that ships per-device variants, which is
    what the store itself shows; ``lookup()`` returns the real version for an app
    that publishes one. A Play search hit never carries a version.

    The two dates are the exception to that convention: they are ``None`` when
    absent, because a date has no honest placeholder. A sentinel would sort, filter
    and diff as though it were real, which is exactly how Play reviews once came
    back dated to 1973.

    See the coverage table in ``docs/reference/models.md``.

    ``price`` is formatted with ``$`` regardless of storefront currency.
    """

    app_id: str
    store: Store
    name: str
    developer: str
    category: str
    price: str
    version: str
    rating: float
    rating_count: int
    url: str
    icon_url: str | None = None

    current_version_release_date: datetime | None = None
    """When the version in ``version`` was published, or None if unknown.

    Precision differs by store, so compare across them with that in mind. iTunes
    sends a real timestamp. Play publishes only the day it renders as "Updated on",
    so this is midnight UTC on that day: the date is the datum and the time is
    padding. Only a Play ``lookup()``, or a featured search hit, carries one at all.
    """

    first_release_date: datetime | None = None
    """When the app first appeared on the store, or None if unknown.

    Same precision caveat as ``current_version_release_date``. Doubles as the floor
    on a review history: no review of this app predates it.
    """

    def __post_init__(self) -> None:
        if not isfinite(self.rating):
            raise ValueError("rating must be finite")
        if not isfinite(self.rating_count):
            raise ValueError("rating_count must be finite")

    def to_dict(self) -> dict[str, Any]:
        """Return every metadata field as a JSON-safe plain dictionary."""
        values: dict[str, Any] = {}
        for model_field in fields(self):
            value = getattr(self, model_field.name)
            values[model_field.name] = (
                value.isoformat() if isinstance(value, datetime) else value
            )
        return values
