"""Ids that land in a URL path may not be dot segments.

``quote`` leaves ``.`` alone, so an id of ``..`` reaches the client as
``/v1/apps/../customerReviews``, which the client normalises into a request for
another endpoint, carrying the caller's credential. Every entry point that puts
an id in a path refuses ``.`` and ``..`` before it sends anything.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from app_reviews import (
    AppStoreAuth,
    AppStoreReplies,
    AppStoreVersions,
    GooglePlayAuth,
    GooglePlayReplies,
)
from app_reviews.appstore.connect import AppStoreOfficialProvider
from app_reviews.appstore.rss import AppStoreScraperProvider
from app_reviews.googleplay.developer_api import GooglePlayOfficialProvider
from tests.app_reviews.factories import (
    TEST_EC_PRIVATE_KEY,
    StaticToken,
    service_account_json,
)
from tests.app_reviews.transport import Recorder, Twin, client_for

Call = Callable[[Twin, Recorder, str], Awaitable[Any]]

DOT_SEGMENTS = [".", ".."]


def _recorder() -> Recorder:
    return Recorder(httpx.Response(200, text="{}"))


def _app_store_auth() -> AppStoreAuth:
    return AppStoreAuth(key_id="K", issuer_id="I", private_key=TEST_EC_PRIVATE_KEY)


def _play_auth() -> GooglePlayAuth:
    return GooglePlayAuth(service_account_info=service_account_json())


async def _connect(twin: Twin, recorder: Recorder, segment: str) -> Any:
    provider = client_for(AppStoreOfficialProvider, recorder, auth=StaticToken())
    return await twin.call(provider, "fetch_page", segment, "", None)


async def _versions(twin: Twin, recorder: Recorder, segment: str) -> Any:
    versions = client_for(AppStoreVersions, recorder, auth=_app_store_auth())
    return await twin.call(versions, "versions", segment)


async def _app_store_reply_read(twin: Twin, recorder: Recorder, segment: str) -> Any:
    replies = client_for(AppStoreReplies, recorder, auth=_app_store_auth())
    return await twin.call(replies, "get_reply", segment)


async def _play_reply_write(twin: Twin, recorder: Recorder, segment: str) -> Any:
    replies = client_for(GooglePlayReplies, recorder, auth=_play_auth())
    return await twin.call(
        replies, "reply", segment, "Thanks!", package_name="com.example"
    )


async def _play_reply_review(twin: Twin, recorder: Recorder, segment: str) -> Any:
    replies = client_for(GooglePlayReplies, recorder, auth=_play_auth())
    return await twin.call(replies, "get_reply", segment, package_name="com.example")


async def _play_reply_package(twin: Twin, recorder: Recorder, segment: str) -> Any:
    replies = client_for(GooglePlayReplies, recorder, auth=_play_auth())
    return await twin.call(replies, "get_reply", "gp-1", package_name=segment)


async def _developer_api(twin: Twin, recorder: Recorder, segment: str) -> Any:
    provider = client_for(GooglePlayOfficialProvider, recorder, auth=StaticToken())
    return await twin.call(provider, "fetch_page", segment, "", None)


async def _rss_app(twin: Twin, recorder: Recorder, segment: str) -> Any:
    provider = client_for(AppStoreScraperProvider, recorder)
    return await twin.call(provider, "fetch_page", segment, "us", None)


async def _rss_country(twin: Twin, recorder: Recorder, segment: str) -> Any:
    provider = client_for(AppStoreScraperProvider, recorder)
    return await twin.call(provider, "fetch_page", "12345", segment, None)


CALLS: dict[str, Call] = {
    "connect-app": _connect,
    "versions-app": _versions,
    "app-store-reply-read": _app_store_reply_read,
    "play-reply-write": _play_reply_write,
    "play-reply-review": _play_reply_review,
    "play-reply-package": _play_reply_package,
    "developer-api-app": _developer_api,
    "rss-app": _rss_app,
    "rss-country": _rss_country,
}


@pytest.mark.parametrize("segment", DOT_SEGMENTS)
@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
async def test_a_dot_segment_is_refused_before_any_request(
    call: Call, segment: str, twin: Twin
) -> None:
    recorder = _recorder()

    with pytest.raises(ValueError, match="must not be"):
        await call(twin, recorder, segment)

    assert recorder.requests == []
