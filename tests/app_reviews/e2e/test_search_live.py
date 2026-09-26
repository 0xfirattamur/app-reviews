"""Live tests for search and lookup, hitting real APIs.

Run with: uv run python -m pytest -m live tests/app_reviews/e2e/test_search_live.py -v
"""

from datetime import UTC, timedelta

import pytest

from app_reviews.appstore.search import AppStoreSearch
from app_reviews.googleplay.search import GooglePlaySearch
from app_reviews.models.metadata import AppMetadata


@pytest.mark.live
class TestAppStoreSearchLive:
    def test_search_returns_results(self):
        results = AppStoreSearch().search("whatsapp", limit=5)
        assert len(results) > 0
        assert all(isinstance(r, AppMetadata) for r in results)
        assert all(r.store == "appstore" for r in results)

    def test_search_respects_limit(self):
        results = AppStoreSearch().search("game", limit=3)
        assert len(results) <= 3

    def test_lookup_known_app(self):
        result = AppStoreSearch().lookup("com.burbn.instagram")
        assert result is not None
        assert result.name != "Unknown"
        assert result.store == "appstore"

    def test_version_history_of_a_known_app(self):
        history = AppStoreSearch().version_history("324684580")

        assert len(history) > 1
        assert all(e.version and not e.version.startswith("Version") for e in history)
        assert all(e.released_at.utcoffset() == timedelta(0) for e in history)
        assert history[0].released_at.tzinfo is UTC
        dates = [e.released_at for e in history]
        assert dates == sorted(dates, reverse=True)


@pytest.mark.live
class TestGooglePlaySearchLive:
    def test_search_returns_results(self):
        results = GooglePlaySearch().search("whatsapp", limit=5)
        assert len(results) > 0
        assert all(isinstance(r, AppMetadata) for r in results)
        assert all(r.store == "googleplay" for r in results)

    def test_search_respects_limit(self):
        results = GooglePlaySearch().search("game", limit=3)
        assert len(results) <= 3

    def test_lookup_known_app(self):
        result = GooglePlaySearch().lookup("com.whatsapp")
        assert result is not None
        assert result.name != "Unknown"
        assert result.store == "googleplay"
