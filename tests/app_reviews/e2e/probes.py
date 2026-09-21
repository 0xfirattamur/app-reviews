"""Shared observations for live App Store RSS probes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app_reviews import Country
from app_reviews.models.result import FetchResult


@dataclass(frozen=True, slots=True)
class ReviewProbe:
    """One independently useful app, optionally scoped to a storefront."""

    name: str
    app_id: str
    country: Country | None = None


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    """The result and human-readable evidence from one live probe."""

    probe: ReviewProbe
    result: FetchResult

    def describe(self) -> str:
        outcome = self.result.outcomes[0] if self.result.outcomes else None
        pages = outcome.pages if outcome else 0
        stopped = outcome.stopped_because if outcome else "unknown"
        error = outcome.error if outcome else None
        if error is None:
            error_text = "none"
        else:
            status = f" (HTTP {error.status})" if error.status is not None else ""
            error_text = f"{error.kind}{status}: {error.message}"
        scope = self.probe.country.value if self.probe.country else "global"
        return (
            f"{self.probe.name} ({self.probe.app_id}, {scope}): "
            f"{len(self.result.reviews)} reviews, pages={pages}, stop={stopped}, "
            f"error={error_text}"
        )


def first_non_empty(
    observations: Iterable[ProbeObservation],
) -> ProbeObservation | None:
    """Return the first probe that produced at least one review."""
    return next((item for item in observations if item.result.reviews), None)


def format_observations(observations: Iterable[ProbeObservation]) -> str:
    """Format every probe on its own bullet for assertion diagnostics."""
    return "\n".join(f"- {item.describe()}" for item in observations)
