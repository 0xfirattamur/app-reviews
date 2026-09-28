"""AppStoreVersions against a mocked App Store Connect transport."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app_reviews import (
    AppStoreAuth,
    AppStoreVersion,
    AppStoreVersions,
    HttpClient,
    NotFoundError,
    ParseError,
)
from tests.app_reviews.appstore.test_auth import _TEST_PRIVATE_KEY

_URL = "https://api.appstoreconnect.apple.com/v1/apps/123/appStoreVersions"


def _version(
    version_id: str,
    version: str,
    created: str,
    *,
    localizations: tuple[str, ...] = (),
    state: str = "READY_FOR_DISTRIBUTION",
    release_type: str = "AFTER_APPROVAL",
    earliest: str | None = None,
) -> dict:
    return {
        "type": "appStoreVersions",
        "id": version_id,
        "attributes": {
            "platform": "IOS",
            "versionString": version,
            "appVersionState": state,
            "releaseType": release_type,
            "earliestReleaseDate": earliest,
            "createdDate": created,
        },
        "relationships": {
            "appStoreVersionLocalizations": {
                "data": [
                    {"type": "appStoreVersionLocalizations", "id": loc}
                    for loc in localizations
                ]
            }
        },
    }


def _localization(loc_id: str, locale: str, whats_new: str | None) -> dict:
    return {
        "type": "appStoreVersionLocalizations",
        "id": loc_id,
        "attributes": {"locale": locale, "whatsNew": whats_new},
    }


def _client(handler) -> AppStoreVersions:
    return AppStoreVersions(
        AppStoreAuth(key_id="K", issuer_id="I", private_key=_TEST_PRIVATE_KEY),
        http=HttpClient(transport=httpx.MockTransport(handler)),
    )


class TestVersions:
    def test_maps_attributes_and_whats_new_per_locale(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "data": [
                        _version(
                            "v2",
                            "2.0",
                            "2026-09-01T08:00:00-07:00",
                            localizations=("l1", "l2", "l3"),
                            release_type="SCHEDULED",
                            earliest="2026-09-10T00:00:00Z",
                        )
                    ],
                    "included": [
                        _localization("l1", "en-US", " Faster sync. \n"),
                        _localization("l2", "de-DE", "Schneller."),
                        _localization("l3", "fr-FR", None),
                    ],
                },
            )

        versions = _client(handler).versions("123")

        assert versions == [
            AppStoreVersion(
                version_id="v2",
                version="2.0",
                platform="IOS",
                state="READY_FOR_DISTRIBUTION",
                release_type="SCHEDULED",
                created_at=datetime(2026, 9, 1, 15, 0, tzinfo=UTC),
                earliest_release_date=datetime(2026, 9, 10, tzinfo=UTC),
                release_notes={"en-US": "Faster sync.", "de-DE": "Schneller."},
            )
        ]
        request = seen[0]
        assert str(request.url).startswith(_URL + "?")
        query = parse_qs(urlsplit(str(request.url)).query)
        assert query["include"] == ["appStoreVersionLocalizations"]
        assert query["fields[appStoreVersionLocalizations]"] == ["locale,whatsNew"]
        fields = query["fields[appStoreVersions]"][0].split(",")
        assert "createdDate" in fields
        assert "appStoreState" not in fields
        assert request.headers["authorization"].startswith("Bearer ")

    def test_follows_links_next_and_sorts_newest_created_first(self):
        pages = [
            {
                "data": [_version("v1", "1.0", "2025-01-01T00:00:00Z")],
                "links": {"next": _URL + "?cursor=2"},
            },
            {"data": [_version("v2", "1.1", "2025-06-01T00:00:00Z")]},
        ]
        urls = []

        def handler(request):
            urls.append(str(request.url))
            return httpx.Response(200, json=pages[len(urls) - 1])

        versions = _client(handler).versions("123")

        assert [v.version for v in versions] == ["1.1", "1.0"]
        assert urls[1] == _URL + "?cursor=2"

    @pytest.mark.parametrize(
        "next_url",
        [
            "https://evil.test/v1/apps/123/appStoreVersions",
            "http://api.appstoreconnect.apple.com/v1/apps/123/appStoreVersions",
            "https://api.appstoreconnect.apple.com/v1/users",
            "https://user@api.appstoreconnect.apple.com/v1/apps/123/appStoreVersions",
        ],
    )
    def test_a_next_link_elsewhere_is_refused_without_following_it(self, next_url):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "data": [_version("v1", "1.0", "2025-01-01T00:00:00Z")],
                    "links": {"next": next_url},
                },
            )

        with pytest.raises(ParseError, match=r"links\.next"):
            _client(handler).versions("123")

        assert len(calls) == 1

    def test_a_repeated_next_link_ends_the_walk(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "data": [_version("v1", "1.0", "2025-01-01T00:00:00Z")],
                    "links": {"next": _URL + "?cursor=same"},
                },
            )

        _client(handler).versions("123")

        assert len(calls) == 2

    def test_an_unknown_app_is_not_found(self):
        with pytest.raises(NotFoundError):
            _client(lambda r: httpx.Response(404)).versions("123")

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"data": [{"id": "v1", "attributes": {"platform": "IOS"}}]},
            {"data": [_version("v1", "1.0", "2025-01-01T00:00:00")]},
        ],
        ids=["no-data", "no-versionString", "naive-date"],
    )
    def test_an_unreadable_page_is_a_parse_error_not_a_partial_list(self, body):
        with pytest.raises(ParseError):
            _client(lambda r: httpx.Response(200, json=body)).versions("123")

    async def test_async_versions_matches(self):
        body = {"data": [_version("v1", "1.0", "2025-01-01T00:00:00Z")]}

        versions = await _client(lambda r: httpx.Response(200, json=body)).aversions(
            "123"
        )

        assert [v.version_id for v in versions] == ["v1"]
