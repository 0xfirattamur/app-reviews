"""The App Store RSS JSON feed's XML fallback.

The fixtures are recorded page-1 answers from 2026-09-28, trimmed to their first
three entries, with reviewer names and profile links replaced:

- ``instagram_us_page1``: the JSON feed answered 200 with no entries while the
  XML feed for the same page had them.
- ``duolingo_us_page1``: both feeds had the same reviews.
- ``spotify_tr_page1``: both feeds were empty.
"""

import dataclasses
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app_reviews import AppStoreReviews, RetryConfig
from app_reviews.appstore.rss import AppStoreScraperProvider
from app_reviews.core.http import HttpClient

FIXTURES = Path(__file__).parents[2] / "fixtures/rss"


def _recorded(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


EMPTY_JSON = _recorded("instagram_us_page1.json")
FULL_XML = _recorded("instagram_us_page1.xml")


def _serving(pages: dict[str, Any], seen: list[str] | None = None):
    """A handler answering ``"<page>/<format>"`` keys, and 404 for anything else.

    A value is a body served with 200, or an ``httpx.Response`` served as is.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url).rsplit("/page=", 1)[1]
        if seen is not None:
            seen.append(key)
        answer = pages.get(key)
        if answer is None:
            return httpx.Response(404, text="")
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, text=answer)

    return handler


def _provider(handler, **kwargs) -> AppStoreScraperProvider:
    return AppStoreScraperProvider(
        http=HttpClient(transport=httpx.MockTransport(handler), **kwargs)
    )


def _client(handler) -> AppStoreReviews:
    return AppStoreReviews(http=HttpClient(transport=httpx.MockTransport(handler)))


def _json_page(count: int, start: int = 0) -> str:
    entries = [
        {
            "id": {"label": f"r{start + i}"},
            "im:rating": {"label": "4"},
            "updated": {"label": "2026-09-26T16:52:44-07:00"},
        }
        for i in range(count)
    ]
    return json.dumps({"feed": {"entry": entries}})


def _comparable(review):
    """A review without the two fields that cannot match across feeds."""
    return dataclasses.replace(review, raw=None, fetched_at=None)


class TestAnEmptyJsonPageFallsBackToXml:
    def test_the_xml_entries_are_used(self):
        seen: list[str] = []
        provider = _provider(_serving({"1/json": EMPTY_JSON, "1/xml": FULL_XML}, seen))

        page = provider.fetch_page("389801252", "us", None)

        assert seen == ["1/json", "1/xml"]
        assert page.feed_format == "xml"
        assert page.error is None
        assert [review.id for review in page.reviews] == [
            "14597478860",
            "14597431382",
            "14597420659",
        ]
        assert page.next_cursor == "2"
        assert page.to_dict()["feed_format"] == "xml"

    async def test_async_matches_sync(self):
        pages = {"1/json": EMPTY_JSON, "1/xml": FULL_XML}

        sync_page = _provider(_serving(pages)).fetch_page("389801252", "us", None)
        async_page = await _provider(_serving(pages)).afetch_page(
            "389801252", "us", None
        )

        assert async_page.feed_format == sync_page.feed_format == "xml"
        assert [_comparable(r) for r in async_page.reviews] == [
            _comparable(r) for r in sync_page.reviews
        ]

    def test_an_unreadable_json_page_falls_back_too(self):
        provider = _provider(
            _serving({"1/json": "<html>busy</html>", "1/xml": FULL_XML})
        )

        page = provider.fetch_page("389801252", "us", None)

        assert page.error is None
        assert page.feed_format == "xml"
        assert len(page.reviews) == 3

    def test_the_outcome_reports_the_xml_feed(self):
        seen: list[str] = []
        client = _client(
            _serving(
                {"1/json": EMPTY_JSON, "1/xml": FULL_XML, "2/json": EMPTY_JSON}, seen
            )
        )

        result = client.fetch("389801252", countries=["us"])

        (outcome,) = result.outcomes
        assert len(result) == 3
        assert outcome.feed_format == "xml"
        assert outcome.stopped_because == "exhausted"
        assert result.to_dict()["outcomes"][0]["feed_format"] == "xml"
        # Page 1 was short, so page 2's empty JSON answer is the feed's end.
        assert seen == ["1/json", "1/xml", "2/json"]

    def test_an_empty_page_after_a_full_one_asks_the_xml_feed(self):
        seen: list[str] = []
        client = _client(
            _serving(
                {
                    "1/json": _json_page(50),
                    "2/json": EMPTY_JSON,
                    "2/xml": FULL_XML,
                    "3/json": EMPTY_JSON,
                },
                seen,
            )
        )

        result = client.fetch("389801252", countries=["us"])

        assert seen == ["1/json", "2/json", "2/xml", "3/json"]
        assert len(result) == 53
        assert result.outcomes[0].feed_format == "xml"

    def test_a_resumed_cursor_may_fall_back_without_a_previous_page(self):
        seen: list[str] = []
        provider = _provider(_serving({"4/json": EMPTY_JSON, "4/xml": FULL_XML}, seen))

        page = provider.fetch_page("389801252", "us", "4")

        assert seen == ["4/json", "4/xml"]
        assert page.feed_format == "xml"

    def test_the_xml_request_goes_through_the_same_client(self):
        class Counting:
            def __init__(self) -> None:
                self.recorded: list[int] = []

            def acquire(self) -> None:
                pass

            async def aacquire(self) -> None:
                pass

            def record(self, status: int, retry_after: float | None) -> None:
                self.recorded.append(status)

        limiter = Counting()
        xml_answers = iter(
            [httpx.Response(503, text=""), httpx.Response(200, text=FULL_XML)]
        )
        provider = _provider(
            _serving_then(EMPTY_JSON, xml_answers),
            rate_limiter=limiter,
            retry=RetryConfig(max_retries=1, retry_on=(503,), backoff_factor=0),
        )

        page = provider.fetch_page("389801252", "us", None)

        assert page.feed_format == "xml"
        assert limiter.recorded == [200, 503, 200]


def _serving_then(json_body: str, xml_answers):
    """JSON page 1 answers ``json_body``; each XML request takes the next answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/json"):
            return httpx.Response(200, text=json_body)
        return next(xml_answers)

    return handler


class TestTheXmlFeedParsesLikeTheJsonFeed:
    def test_the_same_reviews_come_back_field_for_field(self):
        json_page = _provider(
            _serving({"1/json": _recorded("duolingo_us_page1.json")})
        ).fetch_page("570060128", "us", None)
        xml_page = _provider(
            _serving(
                {"1/json": EMPTY_JSON, "1/xml": _recorded("duolingo_us_page1.xml")}
            )
        ).fetch_page("570060128", "us", None)

        assert json_page.feed_format == "json"
        assert xml_page.feed_format == "xml"
        assert len(json_page.reviews) == 3
        assert [_comparable(r) for r in xml_page.reviews] == [
            _comparable(r) for r in json_page.reviews
        ]
        assert xml_page.next_cursor == json_page.next_cursor == "2"

    def test_the_text_content_is_the_body_not_the_html_rendering(self):
        page = _provider(
            _serving(
                {"1/json": EMPTY_JSON, "1/xml": _recorded("duolingo_us_page1.xml")}
            )
        ).fetch_page("570060128", "us", None)

        assert page.reviews[0].body == "This helped me learn Japanese fast"
        assert page.reviews[0].raw["content"] == {
            "label": "This helped me learn Japanese fast",
            "attributes": {"type": "text"},
        }

    def test_a_full_json_page_never_asks_the_xml_feed(self):
        seen: list[str] = []

        page = _provider(
            _serving({"1/json": _recorded("duolingo_us_page1.json")}, seen)
        ).fetch_page("570060128", "us", None)

        assert seen == ["1/json"]
        assert page.feed_format == "json"


class TestBothFeedsEmpty:
    def test_is_a_normal_exhaustion(self):
        seen: list[str] = []
        client = _client(
            _serving(
                {
                    "1/json": _recorded("spotify_tr_page1.json"),
                    "1/xml": _recorded("spotify_tr_page1.xml"),
                },
                seen,
            )
        )

        result = client.fetch("324684580", countries=["tr"])

        (outcome,) = result.outcomes
        assert seen == ["1/json", "1/xml"]
        assert len(result) == 0
        assert outcome.error is None
        assert outcome.stopped_because == "exhausted"
        assert outcome.feed_format == "json"

    def test_the_page_offers_no_cursor(self):
        page = _provider(
            _serving(
                {
                    "1/json": _recorded("spotify_tr_page1.json"),
                    "1/xml": _recorded("spotify_tr_page1.xml"),
                }
            )
        ).fetch_page("324684580", "tr", None)

        assert page.reviews == []
        assert page.next_cursor is None
        assert page.error is None
        assert page.feed_format == "json"


class TestAnUnusableXmlAnswer:
    @pytest.mark.parametrize(
        "xml",
        [
            "<feed><entry>",
            "not xml at all",
            '<?xml version="1.0"?><rss><channel/></rss>',
            '<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY a "aaaa">]>'
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>&a;</id></entry>'
            "</feed>",
        ],
    )
    def test_malformed_xml_leaves_the_json_answer_standing(self, xml, caplog):
        page = _provider(_serving({"1/json": EMPTY_JSON, "1/xml": xml})).fetch_page(
            "389801252", "us", None
        )

        assert page.error is None
        assert page.reviews == []
        assert page.next_cursor is None
        assert page.feed_format == "json"
        assert "Unreadable App Store RSS XML" in caplog.text

    def test_malformed_xml_after_unreadable_json_is_a_parse_error(self):
        page = _provider(
            _serving({"1/json": "<html>busy</html>", "1/xml": "<feed><entry>"})
        ).fetch_page("389801252", "us", None)

        assert page.error is not None
        assert page.error.kind == "parse"
        assert page.feed_format is None

    def test_empty_xml_after_unreadable_json_is_the_feeds_end(self):
        page = _provider(
            _serving(
                {
                    "1/json": "<html>busy</html>",
                    "1/xml": _recorded("spotify_tr_page1.xml"),
                }
            )
        ).fetch_page("324684580", "tr", None)

        assert page.error is None
        assert page.next_cursor is None
        assert page.feed_format == "xml"

    @pytest.mark.parametrize(
        ("status", "kind"), [(403, "rate_limited"), (503, "server"), (404, "not_found")]
    )
    def test_a_failed_xml_request_is_the_pages_failure(self, status, kind):
        page = _provider(
            _serving({"1/json": EMPTY_JSON, "1/xml": httpx.Response(status, text="")})
        ).fetch_page("389801252", "us", None)

        assert page.error is not None
        assert page.error.kind == kind
        assert page.error.status == status
        assert page.feed_format is None

    def test_a_failed_json_request_never_asks_the_xml_feed(self):
        seen: list[str] = []

        page = _provider(
            _serving({"1/json": httpx.Response(503, text=""), "1/xml": FULL_XML}, seen)
        ).fetch_page("389801252", "us", None)

        assert seen == ["1/json"]
        assert page.error is not None
        assert page.error.kind == "server"
