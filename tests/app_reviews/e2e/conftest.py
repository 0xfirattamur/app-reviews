"""Session-scoped live provider observations."""

from __future__ import annotations

import pytest

from app_reviews import AppStoreReviews, Country, GooglePlayReviews
from tests.app_reviews.e2e.probes import ProbeObservation, ReviewProbe

_LIMIT = 20
_APP_STORE_PROBES = (
    ReviewProbe("Google Maps", "585027354", Country.US),
    ReviewProbe("Instagram", "389801252", Country.GB),
    ReviewProbe("Among Us", "1351168404", Country.US),
)

_GOOGLE_PLAY_PROBES = (
    ReviewProbe("Google Maps", "com.google.android.apps.maps"),
    ReviewProbe("Instagram", "com.instagram.android"),
    ReviewProbe("Spotify", "com.spotify.music"),
)


@pytest.fixture(scope="session")
def app_store_observations() -> tuple[ProbeObservation, ...]:
    """Fetch every independent RSS probe once for the entire live suite."""
    client = AppStoreReviews()
    return tuple(
        ProbeObservation(
            probe=probe,
            result=client.fetch(
                probe.app_id,
                countries=[probe.country],
                limit=_LIMIT,
            ),
        )
        for probe in _APP_STORE_PROBES
    )


@pytest.fixture(scope="session")
def google_play_observations() -> tuple[ProbeObservation, ...]:
    """Fetch independent global Play probes once for the live suite."""
    client = GooglePlayReviews()
    return tuple(
        ProbeObservation(
            probe=probe,
            result=client.fetch(probe.app_id, limit=_LIMIT),
        )
        for probe in _GOOGLE_PLAY_PROBES
    )
