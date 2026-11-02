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
    NotFoundError,
    ParseError,
)
from tests.app_reviews.factories import TEST_EC_PRIVATE_KEY
from tests.app_reviews.transport import client_for

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
    auth = AppStoreAuth(key_id="K", issuer_id="I", private_key=TEST_EC_PRIVATE_KEY)
    return client_for(AppStoreVersions, handler, auth=auth)


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

    async def test_follows_links_next_and_sorts_newest_created_first(self, twin):
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

        versions = await twin.call(_client(handler), "versions", "123")

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
    async def test_a_next_link_elsewhere_is_refused_without_following_it(
        self, twin, next_url
    ):
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
            await twin.call(_client(handler), "versions", "123")

        assert len(calls) == 1

    async def test_a_repeated_next_link_is_a_parse_error(self, twin):
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

        with pytest.raises(ParseError, match="repeated"):
            await twin.call(_client(handler), "versions", "123")

        assert len(calls) == 2

    async def test_a_cycle_through_another_page_is_a_parse_error(self, twin):
        cursors = ["a", "b", "a"]
        calls = []

        def handler(request):
            calls.append(request)
            next_url = f"{_URL}?cursor={cursors[len(calls) - 1]}"
            return httpx.Response(
                200,
                json={
                    "data": [_version("v1", "1.0", "2025-01-01T00:00:00Z")],
                    "links": {"next": next_url},
                },
            )

        with pytest.raises(ParseError, match="repeated"):
            await twin.call(_client(handler), "versions", "123")

        assert len(calls) == 3

    async def test_a_walk_past_the_page_ceiling_is_a_parse_error(
        self, twin, monkeypatch
    ):
        monkeypatch.setattr(AppStoreVersions, "MAX_PAGES", 3)
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "data": [_version("v1", "1.0", "2025-01-01T00:00:00Z")],
                    "links": {"next": f"{_URL}?cursor={len(calls)}"},
                },
            )

        with pytest.raises(ParseError, match="more than 3 pages"):
            await twin.call(_client(handler), "versions", "123")

        assert len(calls) == 3

    async def test_an_unknown_app_is_not_found(self, twin):
        with pytest.raises(NotFoundError):
            await twin.call(_client(lambda r: httpx.Response(404)), "versions", "123")

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"data": [{"id": "v1", "attributes": {"platform": "IOS"}}]},
            {"data": [_version("v1", "1.0", "2025-01-01T00:00:00")]},
        ],
        ids=["no-data", "no-versionString", "naive-date"],
    )
    async def test_an_unreadable_page_is_a_parse_error_not_a_partial_list(
        self, twin, body
    ):
        with pytest.raises(ParseError):
            await twin.call(
                _client(lambda r: httpx.Response(200, json=body)), "versions", "123"
            )

    async def test_async_versions_matches(self):
        body = {"data": [_version("v1", "1.0", "2025-01-01T00:00:00Z")]}

        versions = await _client(lambda r: httpx.Response(200, json=body)).aversions(
            "123"
        )

        assert [v.version_id for v in versions] == ["v1"]
