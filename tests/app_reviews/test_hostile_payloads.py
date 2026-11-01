"""Store bodies nested deeper than Python can recurse are parse errors.

``json`` and the Atom reader recurse per nesting level, so a few thousand ``[``
used to escape every parse handler as ``RecursionError``. Each public entry point
that reads a store body must report such a body as ``ParseError`` or a page error
of kind ``parse``, on the sync and async paths alike.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from app_reviews import (
    AppStoreAuth,
    AppStoreReplies,
    GooglePlayAuth,
    GooglePlayReplies,
    ParseError,
)
from app_reviews.appstore.connect import AppStoreOfficialProvider
from app_reviews.appstore.rss import AppStoreScraperProvider
from app_reviews.appstore.search import AppStoreSearch
from app_reviews.googleplay.developer_api import GooglePlayOfficialProvider
from app_reviews.googleplay.search import GooglePlaySearch
from app_reviews.googleplay.web import GooglePlayScraperProvider
from app_reviews.models.page import PageResult
from tests.app_reviews.factories import (
    TEST_EC_PRIVATE_KEY,
    StaticToken,
    service_account_json,
)
from tests.app_reviews.transport import Twin, client_for

NESTING_BEYOND_RECURSION_LIMIT = 100_000
"""Python 3.12's C scanner reads 5000 open brackets as a plain syntax error; it
raises ``RecursionError`` only from tens of thousands."""
DEEP_JSON = "[" * NESTING_BEYOND_RECURSION_LIMIT
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
DEEP_ATOM = (
    f'<feed xmlns="{ATOM_NAMESPACE}"><entry>'
    + "<id>" * 600
    + "</id>" * 600
    + "</entry></feed>"
)
DEEP_PLAY_PAGE = (
    "<html><script>AF_initDataCallback({key: 'ds:1', data:"
    + DEEP_JSON
    + "});</script></html>"
)
DEEP_BATCHEXECUTE_PAYLOAD = (
    ")]}'\n\n" + '[["wrb.fr","oCPfdb","' + DEEP_JSON + '",null,"generic"]]'
)

Case = Callable[[Twin], Awaitable[Any]]


def _serving(body: str) -> Callable[[httpx.Request], httpx.Response]:
    """Answers every request with ``body``, except Google's token exchange."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, text=body)

    return handler


def _rss_xml_only(xml_body: str) -> Callable[[httpx.Request], httpx.Response]:
    """An empty JSON feed, so the provider reads the XML feed instead."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/json"):
            return httpx.Response(200, text='{"feed": {"entry": []}}')
        return httpx.Response(200, text=xml_body)

    return handler


async def _rss_json(twin: Twin) -> Any:
    provider = client_for(AppStoreScraperProvider, _serving(DEEP_JSON))
    return await twin.call(provider, "fetch_page", "1", "us", None)


async def _rss_xml(twin: Twin) -> Any:
    provider = client_for(AppStoreScraperProvider, _rss_xml_only(DEEP_ATOM))
    page = await twin.call(provider, "fetch_page", "1", "us", None)
    return page.xml_fallback_error


async def _connect(twin: Twin) -> Any:
    provider = client_for(
        AppStoreOfficialProvider, _serving(DEEP_JSON), auth=StaticToken()
    )
    return await twin.call(provider, "fetch_page", "1", "", None)


async def _developer_api(twin: Twin) -> Any:
    provider = client_for(
        GooglePlayOfficialProvider, _serving(DEEP_JSON), auth=StaticToken()
    )
    return await twin.call(provider, "fetch_page", "com.example", "", None)


async def _play_web(twin: Twin) -> Any:
    provider = client_for(
        GooglePlayScraperProvider, _serving(DEEP_BATCHEXECUTE_PAYLOAD)
    )
    return await twin.call(provider, "fetch_page", "com.example", "us", None)


async def _play_web_envelope(twin: Twin) -> Any:
    body = ")]}'\n\n" + DEEP_JSON
    provider = client_for(GooglePlayScraperProvider, _serving(body))
    return await twin.call(provider, "fetch_page", "com.example", "us", None)


async def _itunes_search(twin: Twin) -> Any:
    search = client_for(AppStoreSearch, _serving(DEEP_JSON))
    return await twin.call(search, "search", "whatsapp")


async def _itunes_lookup(twin: Twin) -> Any:
    search = client_for(AppStoreSearch, _serving(DEEP_JSON))
    return await twin.call(search, "lookup", "310633997")


async def _product_page(twin: Twin) -> Any:
    page = (
        '<script id="serialized-server-data" type="application/json">'
        + DEEP_JSON
        + "</script>"
    )
    search = client_for(AppStoreSearch, _serving(page))
    return await twin.call(search, "version_history", "310633997")


async def _play_search(twin: Twin) -> Any:
    search = client_for(GooglePlaySearch, _serving(DEEP_PLAY_PAGE))
    return await twin.call(search, "search", "whatsapp")


async def _play_lookup(twin: Twin) -> Any:
    search = client_for(GooglePlaySearch, _serving(DEEP_PLAY_PAGE))
    return await twin.call(search, "lookup", "com.whatsapp")


async def _app_store_reply(twin: Twin) -> Any:
    auth = AppStoreAuth(key_id="K", issuer_id="I", private_key=TEST_EC_PRIVATE_KEY)
    replies = client_for(AppStoreReplies, _serving(DEEP_JSON), auth=auth)
    return await twin.call(replies, "get_reply", "review-1")


async def _play_reply(twin: Twin) -> Any:
    auth = GooglePlayAuth(service_account_info=service_account_json())
    replies = client_for(GooglePlayReplies, _serving(DEEP_JSON), auth=auth)
    return await twin.call(replies, "get_reply", "gp-1", package_name="com.example")


CASES: dict[str, Case] = {
    "rss-json": _rss_json,
    "rss-xml": _rss_xml,
    "connect": _connect,
    "developer-api": _developer_api,
    "play-web-payload": _play_web,
    "play-web-envelope": _play_web_envelope,
    "itunes-search": _itunes_search,
    "itunes-lookup": _itunes_lookup,
    "product-page": _product_page,
    "play-search": _play_search,
    "play-lookup": _play_lookup,
    "app-store-reply": _app_store_reply,
    "play-reply": _play_reply,
}


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
async def test_over_deep_nesting_is_a_parse_error(case: Case, twin: Twin) -> None:
    try:
        outcome = await case(twin)
    except ParseError:
        return

    page_error = outcome.error if isinstance(outcome, PageResult) else outcome
    assert page_error is not None, "the body was read as a success"
    assert page_error.kind == "parse"


HOSTILE_PAGE_TIME_BUDGET_SECONDS = 2.0
"""Linear work on these pages takes milliseconds; the regexes they target took
tens of seconds. The budget sits far from both so a slow machine cannot flake."""

MARKER_COUNT = 20_000

UNCLOSED_PLAY_CALLBACKS = "AF_initDataCallback({key: 'ds:1', data:" * MARKER_COUNT
UNTERMINATED_SCRIPT_TAGS = "<script " * MARKER_COUNT
UNCLOSED_SERVER_DATA_SCRIPTS = (
    '<script id="serialized-server-data" type="application/json">' * MARKER_COUNT
)


async def _play_search_page(twin: Twin, body: str) -> Any:
    search = client_for(GooglePlaySearch, _serving(body))
    return await twin.call(search, "search", "whatsapp")


async def _product_page_history(twin: Twin, body: str) -> Any:
    search = client_for(AppStoreSearch, _serving(body))
    return await twin.call(search, "version_history", "310633997")


HOSTILE_PAGES = {
    "play-unclosed-callbacks": (_play_search_page, UNCLOSED_PLAY_CALLBACKS),
    "product-page-unterminated-tags": (_product_page_history, UNTERMINATED_SCRIPT_TAGS),
    "product-page-unclosed-data": (
        _product_page_history,
        UNCLOSED_SERVER_DATA_SCRIPTS,
    ),
}


@pytest.mark.parametrize(
    ("read_page", "body"), HOSTILE_PAGES.values(), ids=HOSTILE_PAGES.keys()
)
async def test_a_page_of_unclosed_markers_is_parsed_in_linear_time(
    read_page: Callable[[Twin, str], Awaitable[Any]], body: str, twin: Twin
) -> None:
    started = time.perf_counter()

    with pytest.raises(ParseError):
        await read_page(twin, body)

    assert time.perf_counter() - started < HOSTILE_PAGE_TIME_BUDGET_SECONDS
