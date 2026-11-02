"""Test data factories for app-reviews."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app_reviews.core.provider import ReviewProvider
from app_reviews.core.reviews import BaseReviews
from app_reviews.googleplay.auth import ServiceAccountCredentials
from app_reviews.models.config import ConnectCredentials
from app_reviews.models.page import PageResult
from app_reviews.models.review import Review

_BASE_DT = datetime(2024, 3, 15, 10, 0, 0, tzinfo=UTC)

NOW = datetime(2026, 7, 30, tzinfo=UTC)

_PLACEHOLDER_PEM = "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----"

# An EC P-256 (PKCS8) private key, the algorithm App Store Connect signs with.
# For testing only, not real credentials.
TEST_EC_PRIVATE_KEY = """\
-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgmyZUU2SlJ3toFzmc
KjNRLWVEsavmMY1Hj7b1ZrzLLw2hRANCAARRL8UbpZS8HUhpOGIGGtjoeF37luMJ
M7B/Gtt1xwTbLmdGUAZtvYMSMhCVBMel1av5EYUWK24HE7Aj3J+O1AAq
-----END PRIVATE KEY-----
"""

# An RSA 2048 (PKCS8) private key, the algorithm Google service accounts use.
# For testing only, not real credentials.
TEST_RSA_PRIVATE_KEY = """\
-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCsfsIYt4li+qCP
ItkaZbm3f6siEa0ItRF98Il4dwdwn1ahSTZNKiY6IVpdkEJSjancWKSrTkRm8RM4
lCSvuxWhYgAfrNtc5cmFbidxBZOlfCtK+uvzNdPKUGALPSorp5BUWALt64T9i2Sb
nNFZy4uDXzcnry0/+Ktacss9JfUzKX8r8sDa20YRvCBftpwJK/nQeNQXT2IbI2IG
f4r2b4UDcrTusA/CGrC8kzV795R0F1rs+ZZf/Q4xZOButuyjm9JUIyP0gPRoOFnN
v8PhbM4l+InKalbSpgJ4poIpnzoCLtTrPly1wzjuDLVbC1vqTkQN66a/ifu3hkET
bMQ9eEw/AgMBAAECggEACmR+tDeGnwCDoOwUEXuUg2I9QMm415/1JFXUKn5dY+Mt
uHztORvzfqgYxi9VV5zyYXSzwyBtjZgztMJ0LWRDqtv5Lu9erguoJxJabPxNh3hi
MVvJ0HbrQjKyfqraOhQX5YUB9Cyu8Amwb5G0qUjSqIEZlRoAS2zjggkNhqsdSU46
5E6ogKXhkpTrAa7KSNOom5llqk8eAkbpbCxWko7Hh/2RGzBrXvPjEe0fIp4JmL9J
n8fvTP8QCndDhyOB3R4DsKckHyvRECZ5bX89zTuCZEHoACRNfl5X4M3ie+WdV0MU
UGiVl7cDRx67NSX/sfgy2G0NKZxFXQQ3ZOFVd2VAgQKBgQDap7KPQSqzdwl/8+hF
geGrKp0hUZjQOZF2SFxamHYxZiSXFWUspqNgGToARAxR92DE+rrQazw8ikFiIXti
3O8i0AmQEgYoJefLyWRdDPDnwBPhZ/udALarE8VobnzFnj7IRGWddZ6jO3WkXp/9
hxyLuCDxGFKV1+BiIrTZbpmbgQKBgQDJ9Mz2WpzyJASzo1mjOGwnuvYqioh78QF9
EHXvP1ETHmrMuVPMSzM8rluinJBQquIQ1gnMSCwFUwc5z2LT8F8GrOyhH4aM2lE6
/3vEa7Dcp8+R2e+LMVmOn6rY+alLplVp4oices+/u/2UzYfPEoOmmEEPRtgHGVh/
E2wzPvzHvwKBgQDH3xFO9+/TmxV4+/kvSFmDwHFf2pnIhUcdi2m4erNX1pnN7YXN
egnPt1+YjJuCdZHknZkVGWE3qd24mQiTx4dlGCwVoRQ1sOihFNgEYub3wVGq8wVC
cDuKSIlkO9McRdn38OW+OJ0pcxnHhaPT+aSzZz3dhTFAvdPjgPw14W7SgQKBgQDF
osqd38ktcbAkOBx1jbbSHrXepdmxKQZi5KP1MQpfYmnz1P27tG/810iX1w2n08MN
5NiL0Kk7wKgWm+xEKjxkZP/hId/joZbS3Mi7TQQ0vAh23Eb++ZmB6KEiWxihDrMR
vv4f3FPId+lOIODh9WVeuCsbDyJDuDsRiOlfMSnhMwKBgDh3v+7dI2E9pIWDzW6R
BGvqX5FnWN4OCbyl98cSo99qXFnLqifFpVfJjhdxk5uDsD5mqwl9GbhNhDFrSVst
OZyyCpMAriwhjl+6B6UT9xwDaqIq+8wxl4zjn4ekjZuT5q19kNC5kWqBKxZs61hG
sFAt2f21YJ6sthPw/SAGgy8t
-----END PRIVATE KEY-----
"""


_USE_DEFAULT: Any = object()


def make_review(
    *,
    id: str = "scraper-1",
    app_id: str = "12345",
    country: str = "us",
    language: str | None = None,
    rating: int = 5,
    title: str = "Great app",
    body: str = "Love it",
    author_name: str = "Alice",
    app_version: str | None = None,
    created_at: datetime | None = _USE_DEFAULT,
    updated_at: datetime | None = None,
    source: str = "appstore_scraper",
    raw: dict | None = None,
    fetched_at: datetime | None = _USE_DEFAULT,
    store: str = "appstore",
    **other_fields: Any,
) -> Review:
    """Build a Review with sensible defaults. Override any field by keyword.

    Pass ``created_at=None`` to build a review without one; leaving it out gives
    a fixed timestamp. ``other_fields`` carries the optional enrichment fields
    (``developer_reply``, ``device_metadata``, ``thumbs_up_count``, ...).
    """
    return Review(
        store=store,
        app_id=app_id,
        country=country,
        language=language,
        rating=rating,
        title=title,
        body=body,
        author_name=author_name,
        app_version=app_version,
        created_at=_BASE_DT if created_at is _USE_DEFAULT else created_at,
        updated_at=updated_at,
        source=source,
        raw=raw,
        fetched_at=_BASE_DT if fetched_at is _USE_DEFAULT else fetched_at,
        id=id,
        **other_fields,
    )


class FakeProvider:
    """A scripted provider. Records every (country, cursor) it was asked for.

    ``calls`` records every request regardless of entry point, so existing
    assertions on call counts hold for both sync and async walks. ``async_calls``
    records only requests that went through ``afetch_page``, so a test can
    prove the async path actually used it instead of silently falling back to
    the sync ``fetch_page`` body.
    """

    def __init__(
        self, pages: Sequence[PageResult], source: str = "appstore_scraper"
    ) -> None:
        self._pages: Sequence[PageResult] = pages
        self.source: str = source
        self.calls: list[tuple[str, str | None]] = []
        self.async_calls: list[tuple[str, str | None]] = []

    def fetch_page(self, app_id: str, country: str, cursor: str | None) -> PageResult:
        self.calls.append((country, cursor))
        return self._page_for(cursor)

    async def afetch_page(
        self, app_id: str, country: str, cursor: str | None
    ) -> PageResult:
        self.calls.append((country, cursor))
        self.async_calls.append((country, cursor))
        return self._page_for(cursor)

    def _page_for(self, cursor: str | None) -> PageResult:
        index = 0 if cursor is None else int(cursor)
        if index >= len(self._pages):
            return PageResult()
        return self._pages[index]


class MultiCountryProvider:
    """Serves a different scripted page list per country.

    ``calls`` records every request regardless of entry point. ``async_calls``
    records only requests that went through ``afetch_page``, so a test can
    prove the async path actually used it instead of the sync ``fetch_page``
    body.
    """

    def __init__(
        self,
        pages_by_country: Mapping[str, Sequence[PageResult]],
        source: str = "appstore_scraper",
    ) -> None:
        self._pages: Mapping[str, Sequence[PageResult]] = pages_by_country
        self.source: str = source
        self.calls: list[tuple[str, str | None]] = []
        self.async_calls: list[tuple[str, str | None]] = []

    def fetch_page(self, app_id: str, country: str, cursor: str | None) -> PageResult:
        self.calls.append((country, cursor))
        return self._page_for(country, cursor)

    async def afetch_page(
        self, app_id: str, country: str, cursor: str | None
    ) -> PageResult:
        self.calls.append((country, cursor))
        self.async_calls.append((country, cursor))
        return self._page_for(country, cursor)

    def _page_for(self, country: str, cursor: str | None) -> PageResult:
        pages = self._pages.get(country, [])
        index = 0 if cursor is None else int(cursor)
        if index >= len(pages):
            return PageResult()
        return pages[index]


def raw_cursor(cursor: str | None) -> str | None:
    """The provider's own cursor inside a caller-facing ``ar1~<fingerprint>~<raw>``."""
    if cursor is None:
        return None
    _format, _fingerprint, raw = cursor.split("~", 2)
    return raw


class FakeClient(BaseReviews):
    """A review client that walks a fake provider instead of a store."""

    def __init__(self, provider: ReviewProvider) -> None:
        super().__init__()
        self._provider: ReviewProvider = provider

    def _build_provider(self) -> ReviewProvider:
        return self._provider


class StaticToken:
    """A ``TokenSource`` that always returns the same header.

    Providers ask their source per request so an expiring token gets refreshed;
    a test that only cares about parsing wants that to be a constant.
    """

    def __init__(self, header: str = "Bearer test-token") -> None:
        self._header = header

    def authorization_header(self) -> str:
        return self._header

    async def aauthorization_header(self) -> str:
        return self._header


def connect_credentials(**overrides: str) -> ConnectCredentials:
    """Valid App Store Connect credentials. Override any field by keyword."""
    defaults = {
        "key_id": "KEY123",
        "issuer_id": "ISSUER456",
        "private_key": TEST_EC_PRIVATE_KEY,
    }
    return ConnectCredentials(**(defaults | overrides))


def service_account_json(**overrides: Any) -> dict[str, Any]:
    """A Google service-account key document. Override any field by keyword.

    A new dict on every call, so a test may mutate its copy.
    """
    document = {
        "client_email": "test@test.iam.gserviceaccount.com",
        "private_key": TEST_RSA_PRIVATE_KEY,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return document | overrides


def service_account_credentials(**overrides: str) -> ServiceAccountCredentials:
    """Validated service-account credentials. Override any field by keyword.

    The key is a placeholder PEM that passes validation but is not a usable key.
    """
    defaults = {
        "client_email": "test@test.iam.gserviceaccount.com",
        "private_key_pem": _PLACEHOLDER_PEM,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return ServiceAccountCredentials(**(defaults | overrides))


def make_page(
    reviews: Sequence[Review] = (), next_cursor: str | None = None
) -> PageResult:
    """A page of reviews with the cursor that leads to the next one."""
    return PageResult(reviews=list(reviews), next_cursor=next_cursor)
