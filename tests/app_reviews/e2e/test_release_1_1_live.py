"""Live checks for the 1.1.0 App Store additions: the RSS XML fallback and
version history. Each test makes at most two requests.

Run manually with: pytest tests/app_reviews/e2e/test_release_1_1_live.py -m live -v
"""

from __future__ import annotations

import pytest

from app_reviews import AppStoreReviews, AppStoreSearch

pytestmark = pytest.mark.live

DUOLINGO = "570060128"


class TestLiveRssFallback:
    def test_duolingo_us_page_1_returns_reviews(self) -> None:
        with AppStoreReviews() as client:
            page = client.fetch_page(DUOLINGO, country="us")

        seen = f"{len(page.reviews)} reviews from the {page.feed_format} feed"
        print(f"\nDuolingo us page 1: {seen}")
        assert page.error is None, page.error
        assert page.feed_format in ("json", "xml"), seen
        assert len(page.reviews) > 0, seen
        assert all(review.app_id == DUOLINGO for review in page.reviews)


class TestLiveVersionHistory:
    def test_duolingo_history_is_dated_and_carries_release_notes(self) -> None:
        with AppStoreSearch() as client:
            history = client.version_history(DUOLINGO)

        newest = history[0] if history else None
        print(f"\nDuolingo version history: {len(history)} entries; newest {newest}")
        assert history
        assert all(
            entry.released_at.utcoffset().total_seconds() == 0 for entry in history
        )
        dates = [entry.released_at for entry in history]
        assert dates == sorted(dates, reverse=True)
        assert any(entry.release_notes for entry in history)
