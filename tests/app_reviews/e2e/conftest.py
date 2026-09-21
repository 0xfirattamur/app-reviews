"""Session-scoped live provider observations."""

from __future__ import annotations

import pytest

from app_reviews import AppStoreReviews, Country, FetchResult, GooglePlayReviews
from tests.app_reviews.e2e.probes import AppStoreProbe, ProbeObservation

_LIMIT = 20
_GOOGLE_PLAY_APP_ID = "com.google.android.apps.maps"

_APP_STORE_PROBES = (
    AppStoreProbe("Google Maps", "585027354", Country.US),
    AppStoreProbe("Instagram", "389801252", Country.GB),
    AppStoreProbe("Among Us", "1351168404", Country.US),
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
def google_play_result() -> FetchResult:
    """Fetch one global, high-volume Play corpus once for the live suite."""
    return GooglePlayReviews().fetch(_GOOGLE_PLAY_APP_ID, limit=_LIMIT)
