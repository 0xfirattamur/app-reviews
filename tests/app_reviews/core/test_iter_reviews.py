"""Tests for iter_reviews/aiter_reviews: streaming without buffering.

``fetch`` has to hold every review of every country in memory before it returns,
because it filters, sorts and limits across the whole set. With ``Country.ALL``
that is 155 storefronts. ``iter_pages`` streams but hands back pages, so every
caller wrote the same nested loop. This is the rung in between.
"""

import logging
from datetime import timedelta

import pytest

from app_reviews.models.page import PageResult
from app_reviews.models.result import FetchError

from .test_fetch import MultiCountryProvider
from .test_paging import NOW, FakeClient, FakeProvider, _page, _review


class TestIterReviews:
    def test_zero_limit_does_no_io(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        assert list(FakeClient(provider).iter_reviews("123", limit=0)) == []
        assert provider.calls == []

    def test_negative_limit_is_rejected_without_io(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        with pytest.raises(ValueError, match="limit"):
            list(FakeClient(provider).iter_reviews("123", limit=-1))
        assert provider.calls == []

    def test_empty_countries_do_no_io(self):
        provider = FakeProvider([_page([_review(NOW)], None)])

        assert list(FakeClient(provider).iter_reviews("123", countries=[])) == []
        assert provider.calls == []

    def test_yields_individual_reviews_across_pages(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a"), _review(NOW, "b")], "1"),
                _page([_review(NOW, "c")], None),
            ]
        )

        reviews = list(FakeClient(provider).iter_reviews("123", countries=["us"]))

        assert [r.id for r in reviews] == ["a", "b", "c"]

    def test_is_lazy_about_pages(self):
        """The point of the rung: taking one review must not walk the feed."""
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                _page([_review(NOW, "b")], "2"),
                _page([_review(NOW, "c")], None),
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
                "us": [_page([_review(NOW, "us-1")], None)],
                "gb": [_page([_review(NOW, "gb-1")], None)],
            }
        )

        stream = FakeClient(provider).iter_reviews("123", countries=["us", "gb"])
        next(stream)

        assert [country for country, _ in provider.calls] == ["us"]

    def test_spans_countries_in_order(self):
        provider = MultiCountryProvider(
            {
                "us": [_page([_review(NOW, "us-1")], None)],
                "gb": [_page([_review(NOW, "gb-1")], None)],
            }
        )

        reviews = list(FakeClient(provider).iter_reviews("123", countries=["us", "gb"]))

        assert [r.id for r in reviews] == ["us-1", "gb-1"]

    def test_a_global_source_is_walked_once(self):
        provider = FakeProvider(
            [_page([_review(NOW, "a")], None)], source="appstore_official"
        )

        reviews = list(
            FakeClient(provider).iter_reviews("123", countries=["us", "gb", "de"])
        )

        assert [r.id for r in reviews] == ["a"]
        assert [country for country, _ in provider.calls] == [""]

    def test_limit_counts_reviews_yielded_not_pages(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a"), _review(NOW, "b")], "1"),
                _page([_review(NOW, "c")], None),
            ]
        )

        reviews = list(
            FakeClient(provider).iter_reviews("123", countries=["us"], limit=2)
        )

        assert [r.id for r in reviews] == ["a", "b"]

    def test_limit_stops_paging_too(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                _page([_review(NOW, "b")], "2"),
                _page([_review(NOW, "c")], None),
            ]
        )

        list(FakeClient(provider).iter_reviews("123", countries=["us"], limit=1))

        assert len(provider.calls) == 1

    def test_public_max_pages_bounds_the_stream(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                _page([_review(NOW, "b")], "2"),
                _page([_review(NOW, "c")], None),
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
                "us": [_page([_review(NOW, "us-1")], None)],
                "gb": [_page([_review(NOW, "gb-1")], None)],
                "de": [_page([_review(NOW, "de-1")], None)],
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
                _page([_review(NOW, "new")], "1"),
                _page([_review(old, "old")], "2"),
                _page([_review(old, "older")], "3"),
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
                "gb": [_page([_review(NOW, "gb-1")], None)],
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
            _page([_review(NOW, "a"), _review(NOW, "b")], "1"),
            _page([_review(NOW, "c")], None),
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
                _page([_review(NOW, "a")], "1"),
                _page([_review(NOW, "b")], None),
            ]
        )

        stream = FakeClient(provider).aiter_reviews("123", countries=["us"])
        await anext(stream)

        assert len(provider.calls) == 1

    async def test_aiter_reviews_honours_limit(self):
        provider = FakeProvider([_page([_review(NOW, "a"), _review(NOW, "b")], "1")])

        stream = FakeClient(provider).aiter_reviews("123", countries=["us"], limit=1)

        assert [r.id async for r in stream] == ["a"]

    async def test_aiter_reviews_honours_max_pages(self):
        provider = FakeProvider(
            [
                _page([_review(NOW, "a")], "1"),
                _page([_review(NOW, "b")], "2"),
                _page([_review(NOW, "c")], None),
            ]
        )

        stream = FakeClient(provider).aiter_reviews(
            "123", countries=["us"], max_pages=2
        )

        assert [r.id async for r in stream] == ["a", "b"]
        assert len(provider.calls) == 2
