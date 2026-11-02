"""Cursors handed to callers are tied to the app, country and client settings
that issued them, so resuming under different ones fails loudly instead of
returning the wrong pages."""

from collections.abc import Callable
from unittest.mock import Mock

import pytest

from app_reviews import AppStoreReviews
from app_reviews.core.cursor import CursorScope, unwrap_cursor, wrap_cursor
from app_reviews.models.config import AppStoreAuth
from tests.app_reviews.factories import (
    NOW,
    FakeClient,
    FakeProvider,
    make_page,
    make_review,
    raw_cursor,
)

_MISMATCH = "different app, country or client settings"


def _provider() -> FakeProvider:
    return FakeProvider(
        [
            make_page([make_review(created_at=NOW, id="p1")], "1"),
            make_page([make_review(created_at=NOW, id="p2")], "2"),
            make_page([make_review(created_at=NOW, id="p3")], None),
        ]
    )


ConnectClientFactory = Callable[..., AppStoreReviews]


@pytest.fixture
def connect_client(monkeypatch: pytest.MonkeyPatch) -> ConnectClientFactory:
    """Builds Connect clients that walk ``provider``; the patch ends with the test."""

    def build(provider: FakeProvider, **server_options) -> AppStoreReviews:
        auth = AppStoreAuth(key_id="K", issuer_id="I", key_path="p.p8")
        client = AppStoreReviews(auth=auth, **server_options)
        monkeypatch.setattr(client, "_build_provider", Mock(return_value=provider))
        return client

    return build


class TestCursorFormat:
    def test_the_cursor_names_its_format_and_hides_nothing_from_the_provider(self):
        provider = _provider()

        page = FakeClient(provider).fetch_page("123", country="us")

        assert page.next_cursor is not None
        assert page.next_cursor.startswith("ar1~")
        assert raw_cursor(page.next_cursor) == "1"

    def test_the_final_page_has_no_cursor(self):
        provider = _provider()

        page = FakeClient(provider).fetch_page("123", country="us", cursor="2")

        assert page.next_cursor is None

    def test_every_page_of_a_walk_carries_a_wrapped_cursor(self):
        pages = list(FakeClient(_provider()).iter_pages("123", country="us"))

        cursors = [page.next_cursor for page in pages]

        assert [raw_cursor(cursor) for cursor in cursors] == ["1", "2", None]


class TestResuming:
    def test_a_fetched_cursor_resumes_an_iteration(self):
        provider = _provider()
        client = FakeClient(provider)
        first = client.fetch_page("123", country="us")

        pages = list(client.iter_pages("123", country="us", cursor=first.next_cursor))

        assert [r.id for page in pages for r in page.reviews] == ["p2", "p3"]
        assert provider.calls == [("us", None), ("us", "1"), ("us", "2")]

    def test_the_provider_only_ever_sees_raw_cursors(self):
        provider = _provider()
        client = FakeClient(provider)
        first = client.fetch_page("123", country="us")

        client.fetch_page("123", country="us", cursor=first.next_cursor)

        assert provider.calls[-1] == ("us", "1")

    def test_a_cursor_saved_by_1_3_is_passed_through_unchanged(self):
        provider = _provider()

        page = FakeClient(provider).fetch_page("123", country="us", cursor="1")

        assert provider.calls == [("us", "1")]
        assert raw_cursor(page.next_cursor) == "2"

    def test_a_resumed_walk_still_detects_a_repeated_cursor(self):
        looping = FakeProvider([make_page([make_review(created_at=NOW)], "0")])
        client = FakeClient(looping)

        pages = list(client.iter_pages("123", country="us"))

        assert pages[-1].stopped_because == "cycle"

    async def test_the_async_twin_round_trips(self):
        provider = _provider()
        client = FakeClient(provider)
        first = await client.afetch_page("123", country="us")

        pages = [
            page
            async for page in client.aiter_pages(
                "123", country="us", cursor=first.next_cursor
            )
        ]

        assert [r.id for page in pages for r in page.reviews] == ["p2", "p3"]
        assert provider.async_calls == [("us", None), ("us", "1"), ("us", "2")]

    async def test_the_async_twin_accepts_a_1_3_cursor(self):
        provider = _provider()

        await FakeClient(provider).afetch_page("123", country="us", cursor="1")

        assert provider.async_calls == [("us", "1")]


class TestMismatch:
    def _cursor(self) -> str:
        page = FakeClient(_provider()).fetch_page("123", country="us")
        assert page.next_cursor is not None
        return page.next_cursor

    @pytest.mark.parametrize(
        ("app_id", "country"), [("999", "us"), ("123", "gb")], ids=["app", "country"]
    )
    def test_fetch_page_refuses_another_app_or_country(self, app_id, country):
        cursor = self._cursor()
        provider = _provider()

        with pytest.raises(ValueError, match=_MISMATCH) as raised:
            FakeClient(provider).fetch_page(app_id, country=country, cursor=cursor)

        assert provider.calls == []
        assert cursor not in str(raised.value)

    @pytest.mark.parametrize(
        ("app_id", "country"), [("999", "us"), ("123", "gb")], ids=["app", "country"]
    )
    def test_iter_pages_refuses_another_app_or_country(self, app_id, country):
        cursor = self._cursor()
        provider = _provider()

        with pytest.raises(ValueError, match=_MISMATCH):
            list(
                FakeClient(provider).iter_pages(app_id, country=country, cursor=cursor)
            )

        assert provider.calls == []

    @pytest.mark.parametrize(
        ("app_id", "country"), [("999", "us"), ("123", "gb")], ids=["app", "country"]
    )
    async def test_the_async_twins_refuse_another_app_or_country(self, app_id, country):
        cursor = self._cursor()
        provider = _provider()
        client = FakeClient(provider)

        with pytest.raises(ValueError, match=_MISMATCH):
            await client.afetch_page(app_id, country=country, cursor=cursor)
        with pytest.raises(ValueError, match=_MISMATCH):
            async for _ in client.aiter_pages(app_id, country=country, cursor=cursor):
                pass

        assert provider.calls == []

    def test_a_malformed_wrapped_cursor_is_refused(self):
        scope = CursorScope("appstore_scraper", "123", "us")

        with pytest.raises(ValueError, match=_MISMATCH):
            unwrap_cursor("ar1~nofingerprintseparator", scope)

    def test_the_fingerprint_is_stable(self):
        scope = CursorScope("appstore_scraper", "123", "us", options=("a=1",))

        assert wrap_cursor("raw", scope) == wrap_cursor("raw", scope)
        assert unwrap_cursor(wrap_cursor("raw", scope), scope) == "raw"


class TestServerOptions:
    def _cursor(self, connect_client: ConnectClientFactory, **server_options) -> str:
        client = connect_client(_provider(), **server_options)
        page = client.fetch_page("123", country="us")
        assert page.next_cursor is not None
        return page.next_cursor

    @pytest.mark.parametrize(
        "other_options",
        [
            {"server_ratings": [1]},
            {"server_territories": ["us"]},
            {"server_sort": "rating"},
        ],
        ids=["ratings", "territories", "sort"],
    )
    def test_another_server_option_refuses_the_cursor(
        self, connect_client, other_options
    ):
        cursor = self._cursor(connect_client)
        provider = _provider()
        client = connect_client(provider, **other_options)

        with pytest.raises(ValueError, match=_MISMATCH):
            client.fetch_page("123", country="us", cursor=cursor)
        with pytest.raises(ValueError, match=_MISMATCH):
            list(client.iter_pages("123", country="us", cursor=cursor))

        assert provider.calls == []

    def test_the_same_server_options_resume(self, connect_client):
        cursor = self._cursor(connect_client, server_ratings=[1, 2])
        provider = _provider()
        client = connect_client(provider, server_ratings=[2, 1])

        client.fetch_page("123", country="us", cursor=cursor)

        assert provider.calls == [("us", "1")]
