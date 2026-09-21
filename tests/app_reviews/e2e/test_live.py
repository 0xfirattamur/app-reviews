"""Live provider-boundary tests that hit real store endpoints.

These tests are NOT run in normal CI. The scheduled workflow runs them to
detect upstream response changes and parsing regressions. Deterministic
filtering, sorting, limiting, and export behavior belongs in the unit suite.

Run manually with: pytest tests/app_reviews/e2e/test_live.py -m live -v
"""

from __future__ import annotations

import pytest

from app_reviews import Review
from tests.app_reviews.e2e.probes import (
    ProbeObservation,
    first_non_empty,
    format_observations,
)

pytestmark = pytest.mark.live


def _healthy_app_store_probe(
    observations: tuple[ProbeObservation, ...],
) -> ProbeObservation:
    observation = first_non_empty(observations)
    assert observation is not None, (
        "App Store RSS could not be verified: all probes returned zero reviews.\n"
        f"{format_observations(observations)}"
    )
    return observation


def _healthy_google_play_probe(
    observations: tuple[ProbeObservation, ...],
) -> ProbeObservation:
    observation = first_non_empty(observations)
    assert observation is not None, (
        "Google Play could not be verified: all probes returned zero reviews.\n"
        f"{format_observations(observations)}"
    )
    return observation


class TestLiveAppStore:
    """The RSS provider remains reachable, parseable, and able to map reviews."""

    def test_probes_complete_without_provider_errors(
        self, app_store_observations: tuple[ProbeObservation, ...]
    ) -> None:
        failed = [item for item in app_store_observations if item.result.errors]
        assert failed == [], (
            "App Store RSS probes returned provider errors.\n"
            f"{format_observations(app_store_observations)}"
        )

    def test_at_least_one_independent_probe_returns_reviews(
        self, app_store_observations: tuple[ProbeObservation, ...]
    ) -> None:
        _healthy_app_store_probe(app_store_observations)

    def test_maps_core_review_fields(
        self, app_store_observations: tuple[ProbeObservation, ...]
    ) -> None:
        observation = _healthy_app_store_probe(app_store_observations)
        review = observation.result.reviews[0]

        assert isinstance(review, Review)
        assert review.store == "appstore"
        assert review.app_id == observation.probe.app_id
        assert review.country == observation.probe.country.value
        assert review.source == "appstore_scraper"
        assert review.id
        assert 1 <= review.rating <= 5
        assert review.body
        assert review.author_name
        assert review.dated_at is not None


class TestLiveGooglePlay:
    """The batchexecute provider remains reachable and parseable."""

    def test_probes_complete_without_provider_errors(
        self, google_play_observations: tuple[ProbeObservation, ...]
    ) -> None:
        failed = [item for item in google_play_observations if item.result.errors]
        assert failed == [], (
            "Google Play probes returned provider errors.\n"
            f"{format_observations(google_play_observations)}"
        )

    def test_at_least_one_independent_probe_returns_reviews(
        self, google_play_observations: tuple[ProbeObservation, ...]
    ) -> None:
        _healthy_google_play_probe(google_play_observations)

    def test_maps_core_review_fields(
        self, google_play_observations: tuple[ProbeObservation, ...]
    ) -> None:
        observation = _healthy_google_play_probe(google_play_observations)
        review = observation.result.reviews[0]

        assert isinstance(review, Review)
        assert review.store == "googleplay"
        assert review.app_id == observation.probe.app_id
        assert review.source == "googleplay_scraper"
        assert review.id
        assert 1 <= review.rating <= 5
        assert review.body is not None
        assert review.author_name
        assert review.dated_at is not None


class TestLiveCrossStore:
    """Both live providers still expose the same public Review shape."""

    def test_same_json_keys(
        self,
        app_store_observations: tuple[ProbeObservation, ...],
        google_play_observations: tuple[ProbeObservation, ...],
    ) -> None:
        app_store = _healthy_app_store_probe(app_store_observations).result
        google_play = _healthy_google_play_probe(google_play_observations).result

        assert set(app_store.to_dicts()[0]) == set(google_play.to_dicts()[0])
