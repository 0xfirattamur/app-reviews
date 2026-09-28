"""Tests for AppStoreSearch.version_history, read from the App Store product page."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app_reviews import AppVersionEntry
from app_reviews.appstore.search import AppStoreSearch
from app_reviews.core.http import HttpClient
from app_reviews.errors import ParseError, ServerError
from app_reviews.models.config import RetryConfig

FIXTURE = Path(__file__).parents[2] / "fixtures/appstore/version_history.html"
"""The Spotify product page (us), trimmed to the embedded data the parser reads."""

SPOTIFY_NOTES = (
    "We\u2019re always making changes and improvements to Spotify. To make sure you "
    "don\u2019t miss a thing, just keep your Updates turned on."
)


def _item(
    version: Any = "1.0",
    date: Any = "Fri Sep 18 2026 06:25:38 GMT+0000 (Coordinated Universal Time)",
    release_notes: Any = "Notes",
) -> dict[str, Any]:
    return {
        "$kind": "TitledParagraph",
        "text": release_notes,
        "primarySubtitle": version,
        "secondarySubtitle": date,
    }


def _page(page_data: Any = None, *, with_history: bool = True) -> str:
    """A product page whose embedded data holds ``page_data`` as its history."""
    action: dict[str, Any] = {"$kind": "flowAction", "title": "See All"}
    if with_history:
        action |= {"page": "versionHistory", "pageData": page_data}
    data = {"data": [{"data": {"shelfMapping": {"x": {"seeAllAction": action}}}}]}
    return (
        '<html><body><script type="application/json" id="serialized-server-data">'
        f"{json.dumps(data)}</script></body></html>"
    )


def _history(*items: Any) -> str:
    return _page({"shelves": [{"$kind": "Shelf", "items": list(items)}]})


def _client(handler, retry: RetryConfig | None = None) -> AppStoreSearch:
    return AppStoreSearch(
        http=HttpClient(
            transport=httpx.MockTransport(handler),
            retry=retry or RetryConfig(max_retries=0),
        )
    )


def _serving(text: str, status: int = 200) -> AppStoreSearch:
    return _client(lambda _request: httpx.Response(status, text=text))


class TestRecordedPage:
    def test_every_version_is_read_newest_first(self):
        history = _serving(FIXTURE.read_text(encoding="utf-8")).version_history(
            "324684580"
        )

        assert len(history) == 25
        assert history[0] == AppVersionEntry(
            version="9.1.86",
            released_at=datetime(2026, 9, 23, 17, 49, 27, tzinfo=UTC),
            release_notes=SPOTIFY_NOTES,
        )
        assert history[1].version == "9.1.84"
        assert history[1].released_at == datetime(2026, 9, 18, 6, 25, 38, tzinfo=UTC)
        assert history[-1].version == "9.1.30"
        dates = [entry.released_at for entry in history]
        assert dates == sorted(dates, reverse=True)
        assert all(
            entry.released_at.utcoffset().total_seconds() == 0 for entry in history
        )

    async def test_async_matches_sync(self):
        page = FIXTURE.read_text(encoding="utf-8")

        sync_history = _serving(page).version_history("324684580")
        async_history = await _serving(page).aversion_history("324684580")

        assert async_history == sync_history


class TestRequest:
    def test_requests_the_storefront_product_page_as_a_browser(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["agent"] = request.headers["User-Agent"]
            return httpx.Response(200, text=_history(_item()))

        _client(handler).version_history("324684580", country="GB")

        assert seen["url"] == "https://apps.apple.com/gb/app/id324684580"
        assert "Safari" in seen["agent"]

    async def test_async_sends_the_same_request(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, text=_history(_item()))

        await _client(handler).aversion_history("324684580", country="jp")

        assert seen == ["https://apps.apple.com/jp/app/id324684580"]

    def test_follows_the_redirect_to_the_slugged_page(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/us/app/id1":
                return httpx.Response(
                    301, headers={"Location": "https://apps.apple.com/us/app/a/id1"}
                )
            return httpx.Response(200, text=_history(_item("2.0")))

        assert [e.version for e in _client(handler).version_history("1")] == ["2.0"]

    def test_retries_through_the_client_retry_policy(self):
        statuses = iter([503, 200])

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(next(statuses), text=_history(_item("3.0")))

        client = _client(handler, RetryConfig(max_retries=1, backoff_factor=0))

        assert [e.version for e in client.version_history("1")] == ["3.0"]

    @pytest.mark.parametrize("app_id", ["com.spotify.client", "", "id324684580"])
    def test_a_non_numeric_id_is_rejected_without_a_request(self, app_id):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request expected")

        with pytest.raises(ValueError, match="numeric App Store trackId"):
            _client(handler).version_history(app_id)


class TestEntries:
    def test_a_version_label_prefix_is_stripped(self):
        history = _serving(_history(_item("Version 9.1.84"))).version_history("1")

        assert [entry.version for entry in history] == ["9.1.84"]

    def test_a_non_utc_offset_is_normalised_to_utc(self):
        page = _history(_item(date="Fri Sep 18 2026 08:25:38 GMT+0200 (CEST)"))

        (entry,) = _serving(page).version_history("1")

        assert entry.released_at == datetime(2026, 9, 18, 6, 25, 38, tzinfo=UTC)
        assert entry.released_at.tzinfo is UTC

    @pytest.mark.parametrize("release_notes", ["", "   ", None, []])
    def test_missing_release_notes_are_none(self, release_notes):
        (entry,) = _serving(
            _history(_item(release_notes=release_notes))
        ).version_history("1")

        assert entry.release_notes is None

    def test_out_of_order_entries_come_back_newest_first(self):
        page = _history(
            _item("1.0", "Mon Jan 05 2026 10:00:00 GMT+0000"),
            _item("1.2", "Sun Mar 01 2026 10:00:00 GMT+0000"),
            _item("1.1", "Sun Feb 01 2026 10:00:00 GMT+0000"),
        )

        history = _serving(page).version_history("1")

        assert [entry.version for entry in history] == ["1.2", "1.1", "1.0"]


class TestNothingToList:
    def test_an_unknown_app_is_an_empty_history_like_lookup_returns_none(self):
        assert _serving("<html>Not Found</html>", status=404).version_history("1") == []

    def test_a_page_without_a_history_block_is_empty(self):
        assert _serving(_page(with_history=False)).version_history("1") == []

    def test_a_history_with_no_entries_is_empty(self):
        assert _serving(_history()).version_history("1") == []


class TestUnreadablePagesRaise:
    """A partial or empty answer would pass for a real history, so these raise."""

    @pytest.mark.parametrize(
        "body",
        [
            "<html><body>Please enable JavaScript</body></html>",
            '<script type="application/json" id="serialized-server-data">{</script>',
        ],
        ids=["no-embedded-data", "unreadable-embedded-data"],
    )
    def test_a_page_whose_data_cannot_be_read(self, body):
        with pytest.raises(ParseError):
            _serving(body).version_history("1")

    @pytest.mark.parametrize(
        "page",
        [
            _page(None),
            _page({"shelves": {"items": []}}),
            _page({"shelves": [{"$kind": "Shelf"}]}),
            _page({"shelves": ["shelf"]}),
            _history(_item(), "entry"),
            _history(_item(), _item(version=None)),
            _history(_item(), _item(version="Version ")),
            _history(_item(), _item(date=None)),
            _history(_item(), _item(date="2026-09-18T06:25:38Z")),
            _history(_item(), _item(date="Fri Sep 18 2026 06:25:38")),
        ],
        ids=[
            "null-page-data",
            "shelves-not-a-list",
            "shelf-without-items",
            "shelf-not-an-object",
            "entry-not-an-object",
            "entry-without-version",
            "entry-with-an-empty-version",
            "entry-without-date",
            "entry-with-an-iso-date",
            "entry-with-a-date-missing-its-offset",
        ],
    )
    def test_a_history_block_that_cannot_be_read(self, page):
        with pytest.raises(ParseError):
            _serving(page).version_history("1")

    async def test_async_raises_the_same(self):
        with pytest.raises(ParseError):
            await _serving(_history(_item(date=None))).aversion_history("1")

    def test_a_server_failure_is_not_an_empty_history(self):
        with pytest.raises(ServerError):
            _serving("", status=500).version_history("1")
