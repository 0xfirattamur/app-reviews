"""Tests for iter_reviews/aiter_reviews: streaming without buffering.

``fetch`` has to hold every review of every country in memory before it returns,
because it filters, sorts and limits across the whole set. With ``Country.ALL``
that is 155 storefronts. ``iter_pages`` streams but hands back pages, so every
caller wrote the same nested loop. This is the rung in between.
"""

import contextlib
import logging
from datetime import timedelta

import pytest

from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError
from tests.app_reviews.factories import (
    NOW,
    FakeClient,
    FakeProvider,
    MultiCountryProvider,
    make_page,
    make_review,
)


class TestIterReviews:
    def test_zero_limit_does_no_io(self):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])

        assert list(FakeClient(provider).iter_reviews("123", limit=0)) == []
        assert provider.calls == []

    def test_negative_limit_is_rejected_without_io(self):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])

        with pytest.raises(ValueError, match="limit"):
            list(FakeClient(provider).iter_reviews("123", limit=-1))
        assert provider.calls == []

    def test_empty_countries_do_no_io(self):
        provider = FakeProvider([make_page([make_review(created_at=NOW)], None)])

        assert list(FakeClient(provider).iter_reviews("123", countries=[])) == []
        assert provider.calls == []

    def test_yields_individual_reviews_across_pages(self):
        provider = FakeProvider(
            [
                make_page(
                    [
                        make_review(created_at=NOW, id="a"),
                        make_review(created_at=NOW, id="b"),
                    ],
                    "1",
                ),
                make_page([make_review(created_at=NOW, id="c")], None),
            ]
        )

        reviews = list(FakeClient(provider).iter_reviews("123", countries=["us"]))

        assert [r.id for r in reviews] == ["a", "b", "c"]

    def test_is_lazy_about_pages(self):
        """The point of the rung: taking one review must not walk the feed."""
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="a")], "1"),
                make_page([make_review(created_at=NOW, id="b")], "2"),
                make_page([make_review(created_at=NOW, id="c")], None),
            ]
        )

        stream = FakeClient(provider).iter_reviews("123", countries=["us"])
        first = next(stream)

        assert first.id == "a"
        assert len(provider.calls) == 1

    def test_is_lazy_about_countries(self):
        """``fetch`` fans out to every country up front; this must not."""
        provider = MultiCountryProvider(
            {
                "us": [make_page([make_review(created_at=NOW, id="us-1")], None)],
                "gb": [make_page([make_review(created_at=NOW, id="gb-1")], None)],
            }
        )

        stream = FakeClient(provider).iter_reviews("123", countries=["us", "gb"])
        next(stream)

        assert [country for country, _ in provider.calls] == ["us"]

    def test_spans_countries_in_order(self):
        provider = MultiCountryProvider(
            {
                "us": [make_page([make_review(created_at=NOW, id="us-1")], None)],
                "gb": [make_page([make_review(created_at=NOW, id="gb-1")], None)],
            }
        )

        reviews = list(FakeClient(provider).iter_reviews("123", countries=["us", "gb"]))

        assert [r.id for r in reviews] == ["us-1", "gb-1"]

    def test_a_global_source_is_walked_once(self):
        provider = FakeProvider(
            [make_page([make_review(created_at=NOW, id="a")], None)],
            source="appstore_official",
        )

        reviews = list(
            FakeClient(provider).iter_reviews("123", countries=["us", "gb", "de"])
        )

        assert [r.id for r in reviews] == ["a"]
        assert [country for country, _ in provider.calls] == [""]

    def test_limit_counts_reviews_yielded_not_pages(self):
        provider = FakeProvider(
            [
                make_page(
                    [
                        make_review(created_at=NOW, id="a"),
                        make_review(created_at=NOW, id="b"),
                    ],
                    "1",
                ),
                make_page([make_review(created_at=NOW, id="c")], None),
            ]
        )

        reviews = list(
            FakeClient(provider).iter_reviews("123", countries=["us"], limit=2)
        )

        assert [r.id for r in reviews] == ["a", "b"]

    def test_limit_stops_paging_too(self):
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="a")], "1"),
                make_page([make_review(created_at=NOW, id="b")], "2"),
                make_page([make_review(created_at=NOW, id="c")], None),
            ]
        )

        list(FakeClient(provider).iter_reviews("123", countries=["us"], limit=1))

        assert len(provider.calls) == 1

    def test_public_max_pages_bounds_the_stream(self):
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="a")], "1"),
                make_page([make_review(created_at=NOW, id="b")], "2"),
                make_page([make_review(created_at=NOW, id="c")], None),
            ]
        )

        reviews = list(
            FakeClient(provider).iter_reviews("123", countries=["us"], max_pages=2)
        )

        assert [review.id for review in reviews] == ["a", "b"]
        assert len(provider.calls) == 2

    def test_limit_spans_countries(self):
        provider = MultiCountryProvider(
            {
                "us": [make_page([make_review(created_at=NOW, id="us-1")], None)],
                "gb": [make_page([make_review(created_at=NOW, id="gb-1")], None)],
                "de": [make_page([make_review(created_at=NOW, id="de-1")], None)],
            }
        )

        reviews = list(
            FakeClient(provider).iter_reviews(
                "123", countries=["us", "gb", "de"], limit=2
            )
        )

        assert [r.id for r in reviews] == ["us-1", "gb-1"]

    def test_since_stops_the_walk_early_and_filters_the_boundary_page(self):
        """The walk stops *on* the page that crosses ``since``, so that page's
        older reviews have to be dropped rather than yielded.

        This asserted ``["new", "old"]`` before, pinning the leak as expected
        behaviour, which is how it survived. ``old`` is 30 days back with
        ``since`` at 2 days, so it was never inside the window.
        """
        old = NOW - timedelta(days=30)
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="new")], "1"),
                make_page([make_review(created_at=old, id="old")], "2"),
                make_page([make_review(created_at=old, id="older")], "3"),
            ]
        )

        reviews = list(
            FakeClient(provider).iter_reviews(
                "123", countries=["us"], since=NOW - timedelta(days=2)
            )
        )

        assert [r.id for r in reviews] == ["new"]
        assert len(provider.calls) == 2

    def test_a_failed_country_is_logged_and_does_not_raise(self, caplog):
        """A generator has nowhere to put a FetchError, so it warns rather than
        dropping the failure silently. Callers who need the error as data should
        use ``fetch`` or ``iter_pages``.
        """
        err = FetchError(country="us", message="boom", kind="transport")
        provider = MultiCountryProvider(
            {
                "us": [PageResult(error=err)],
                "gb": [make_page([make_review(created_at=NOW, id="gb-1")], None)],
            }
        )

        with caplog.at_level(logging.WARNING):
            reviews = list(
                FakeClient(provider).iter_reviews("123", countries=["us", "gb"])
            )

        assert [r.id for r in reviews] == ["gb-1"]
        assert "boom" in caplog.text


class TestAsyncParity:
    async def test_aiter_reviews_matches_iter_reviews(self):
        pages = [
            make_page(
                [
                    make_review(created_at=NOW, id="a"),
                    make_review(created_at=NOW, id="b"),
                ],
                "1",
            ),
            make_page([make_review(created_at=NOW, id="c")], None),
        ]

        sync = list(
            FakeClient(FakeProvider(pages)).iter_reviews("123", countries=["us"])
        )
        provider = FakeProvider(pages)
        stream = FakeClient(provider).aiter_reviews("123", countries=["us"])
        got = [r async for r in stream]

        assert [r.id for r in got] == [r.id for r in sync]
        assert provider.async_calls  # actually used the async path

    async def test_aiter_reviews_is_lazy(self):
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="a")], "1"),
                make_page([make_review(created_at=NOW, id="b")], None),
            ]
        )

        stream = FakeClient(provider).aiter_reviews("123", countries=["us"])
        await anext(stream)

        assert len(provider.calls) == 1

    async def test_aiter_reviews_honours_limit(self):
        provider = FakeProvider(
            [
                make_page(
                    [
                        make_review(created_at=NOW, id="a"),
                        make_review(created_at=NOW, id="b"),
                    ],
                    "1",
                )
            ]
        )

        stream = FakeClient(provider).aiter_reviews("123", countries=["us"], limit=1)

        assert [r.id async for r in stream] == ["a"]

    async def test_aiter_reviews_honours_max_pages(self):
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="a")], "1"),
                make_page([make_review(created_at=NOW, id="b")], "2"),
                make_page([make_review(created_at=NOW, id="c")], None),
            ]
        )

        stream = FakeClient(provider).aiter_reviews(
            "123", countries=["us"], max_pages=2
        )

        assert [r.id async for r in stream] == ["a", "b"]
        assert len(provider.calls) == 2


class TestOnPage:
    """``on_page`` gives a stream the per-page diagnostics ``fetch`` keeps on its
    outcomes: the country, the page, and any error, as it happens."""

    def test_it_runs_before_the_pages_reviews_are_yielded(self):
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="a")], "1"),
                make_page([make_review(created_at=NOW, id="b")], None),
            ]
        )
        seen: list[str | None] = []

        stream = FakeClient(provider).iter_reviews(
            "123",
            countries=["us"],
            on_page=lambda country, page: seen.append(page.next_cursor),
        )
        next(stream)

        assert seen == ["1"]
        assert len(provider.calls) == 1

    def test_failed_and_empty_pages_are_reported_with_their_country(self):
        error = FetchError(country="us", message="boom", kind="transport")
        provider = MultiCountryProvider(
            {
                "us": [PageResult(error=error)],
                "gb": [make_page([], None)],
            }
        )
        seen: list[tuple[str | None, PageResult]] = []

        reviews = list(
            FakeClient(provider).iter_reviews(
                "123",
                countries=["us", "gb"],
                on_page=lambda country, page: seen.append((country, page)),
            )
        )

        assert reviews == []
        assert [country for country, _ in seen] == ["us", "gb"]
        assert seen[0][1].error is error
        assert seen[0][1].stopped_because == "error"
        assert seen[1][1].stopped_because == "exhausted"

    def test_a_global_source_reports_no_country(self):
        provider = FakeProvider(
            [make_page([make_review(created_at=NOW, id="a")], None)],
            source="appstore_official",
        )
        countries: list[str | None] = []

        list(
            FakeClient(provider).iter_reviews(
                "123", on_page=lambda country, page: countries.append(country)
            )
        )

        assert countries == [None]

    def test_page_rows_are_counted_before_since_trims_them(self):
        old = NOW - timedelta(days=10)
        provider = FakeProvider(
            [
                make_page(
                    [
                        make_review(created_at=NOW, id="new"),
                        make_review(created_at=old, id="old"),
                    ],
                    None,
                )
            ]
        )
        row_counts: list[int] = []

        reviews = list(
            FakeClient(provider).iter_reviews(
                "123",
                countries=["us"],
                since=NOW - timedelta(days=1),
                on_page=lambda country, page: row_counts.append(len(page.reviews)),
            )
        )

        assert [r.id for r in reviews] == ["new"]
        assert row_counts == [2]

    def test_an_exception_from_the_callback_ends_the_stream(self):
        provider = FakeProvider(
            [
                make_page([make_review(created_at=NOW, id="a")], "1"),
                make_page([make_review(created_at=NOW, id="b")], None),
            ]
        )

        def refuse(country, page):
            raise RuntimeError("stop")

        with pytest.raises(RuntimeError, match="stop"):
            list(
                FakeClient(provider).iter_reviews(
                    "123", countries=["us"], on_page=refuse
                )
            )
        assert len(provider.calls) == 1

    async def test_the_async_twin_reports_the_same_pages(self):
        pages = [
            make_page([make_review(created_at=NOW, id="a")], "1"),
            make_page([make_review(created_at=NOW, id="b")], None),
        ]
        sync_seen: list[tuple[str | None, str | None]] = []
        async_seen: list[tuple[str | None, str | None]] = []

        list(
            FakeClient(FakeProvider(pages)).iter_reviews(
                "123",
                countries=["us"],
                on_page=lambda country, page: sync_seen.append(
                    (country, page.next_cursor)
                ),
            )
        )
        [
            review
            async for review in FakeClient(FakeProvider(pages)).aiter_reviews(
                "123",
                countries=["us"],
                on_page=lambda country, page: async_seen.append(
                    (country, page.next_cursor)
                ),
            )
        ]

        assert async_seen == sync_seen == [("us", "1"), ("us", None)]


class _WalkTracingClient(FakeClient):
    """Records when each page walk is closed, to prove nothing waits for the GC."""

    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.closed_walks: list[str] = []

    async def _awalk(self, provider, app_id, country, *args, **kwargs):
        walk = super()._awalk(provider, app_id, country, *args, **kwargs)
        try:
            async with contextlib.aclosing(walk):
                async for page in walk:
                    yield page
        finally:
            self.closed_walks.append(country)


def _two_pages_of_two_reviews() -> FakeProvider:
    first = [make_review(created_at=NOW, id="a"), make_review(created_at=NOW, id="b")]
    second = [make_review(created_at=NOW, id="c"), make_review(created_at=NOW, id="d")]
    return FakeProvider([make_page(first, "1"), make_page(second, None)])


class TestAsyncStreamsCloseTheirInnerGenerators:
    """An async generator abandoned mid-iteration is closed by the event loop at
    some later point, not by the code that abandoned it, so an early exit left
    the page walk (and its pooled request) suspended past the stream's end."""

    async def test_aiter_reviews_closes_the_walk_when_limit_is_reached(self):
        client = _WalkTracingClient(_two_pages_of_two_reviews())

        reviews = [
            review
            async for review in client.aiter_reviews("123", countries=["us"], limit=1)
        ]

        assert [review.id for review in reviews] == ["a"]
        assert client.closed_walks == ["us"]

    async def test_aiter_pages_closes_the_walk_when_the_consumer_stops(self):
        client = _WalkTracingClient(_two_pages_of_two_reviews())

        async with contextlib.aclosing(
            client.aiter_pages("123", country="us")
        ) as pages:
            async for _page in pages:
                break

        assert client.closed_walks == ["us"]
