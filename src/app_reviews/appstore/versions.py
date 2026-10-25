"""An app's versions from App Store Connect.

Endpoint: ``GET /v1/apps/{id}/appStoreVersions``, with the versions'
``appStoreVersionLocalizations`` included for their ``whatsNew``:
https://developer.apple.com/documentation/appstoreconnectapi/get-v1-apps-_id_-appstoreversions

Attributes, per
https://developer.apple.com/documentation/appstoreconnectapi/appstoreversion/attributes-data.dictionary:
``versionString``, ``platform``, ``appVersionState``, ``releaseType``,
``earliestReleaseDate`` and ``createdDate``. The deprecated ``appStoreState`` is
not requested. None of them is the date a version was released; see
``AppStoreVersion``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from app_reviews.appstore.api import ConnectAPIClient
from app_reviews.core.classify import raise_for_http_failure
from app_reviews.core.http import HttpResponse
from app_reviews.errors import ParseError
from app_reviews.models.version import AppStoreVersion

_API = "the App Store Connect API"

_QUERY = urlencode(
    {
        "limit": "200",
        "include": "appStoreVersionLocalizations",
        "limit[appStoreVersionLocalizations]": "50",
        "fields[appStoreVersions]": ",".join(
            (
                "versionString",
                "platform",
                "appVersionState",
                "releaseType",
                "earliestReleaseDate",
                "createdDate",
                "appStoreVersionLocalizations",
            )
        ),
        "fields[appStoreVersionLocalizations]": "locale,whatsNew",
    },
    safe=",[]",
)

_UNUSABLE = (AttributeError, KeyError, TypeError, ValueError)

_OLDEST = datetime.min.replace(tzinfo=UTC)


class AppStoreVersions(ConnectAPIClient):
    """Lists an app's App Store versions from the official API.

    Needs a key that can read the app (any App Store Connect role does).
    """

    def versions(self, app_id: str) -> list[AppStoreVersion]:
        """Every version Connect has for ``app_id``, newest ``created_at`` first.

        ``app_id`` is the numeric Apple ID. Raises ``NotFoundError`` for an app
        the key cannot see, and ``ParseError`` rather than returning a partial
        list when a page cannot be read.
        """
        found: list[AppStoreVersion] = []
        url: str | None = self._first_url(app_id)
        seen: set[str] = set()
        while url is not None and url not in seen:
            seen.add(url)
            response = self._http.get(url, headers=self._headers())
            page, url = self._parse_versions_page(response, app_id)
            found += page
        return self._sort_newest_first(found)

    async def aversions(self, app_id: str) -> list[AppStoreVersion]:
        """Async equivalent of ``versions``."""
        found: list[AppStoreVersion] = []
        url: str | None = self._first_url(app_id)
        seen: set[str] = set()
        while url is not None and url not in seen:
            seen.add(url)
            response = await self._http.aget(url, headers=await self._aheaders())
            page, url = self._parse_versions_page(response, app_id)
            found += page
        return self._sort_newest_first(found)

    def _first_url(self, app_id: str) -> str:
        """Escaped: the id lands in the path of a request carrying the JWT."""
        return f"{self.API_BASE}{self._path(app_id)}?{_QUERY}"

    def _path(self, app_id: str) -> str:
        return f"/v1/apps/{quote(app_id, safe='')}/appStoreVersions"

    def _parse_versions_page(
        self, response: HttpResponse, app_id: str
    ) -> tuple[list[AppStoreVersion], str | None]:
        raise_for_http_failure(response, _API)
        try:
            body = response.json()
            notes = self._notes_by_localization(body.get("included") or [])
            versions = [self._parse_version(entry, notes) for entry in body["data"]]
            next_url = self._read_next_cursor(body, app_id)
        except _UNUSABLE as exc:
            raise ParseError(
                f"Malformed appStoreVersions page from {_API}: {exc}",
                status=response.status,
            ) from exc
        return versions, next_url

    def _notes_by_localization(self, included: list[Any]) -> dict[str, tuple[str, str]]:
        """``{localization id: (locale, whatsNew)}`` for localizations with text."""
        notes: dict[str, tuple[str, str]] = {}
        for included_entry in included:
            if included_entry.get("type") != "appStoreVersionLocalizations":
                continue
            attributes = included_entry.get("attributes") or {}
            text = attributes.get("whatsNew")
            if isinstance(text, str) and text.strip():
                notes[included_entry["id"]] = (str(attributes["locale"]), text.strip())
        return notes

    def _parse_version(
        self, entry: Any, notes: dict[str, tuple[str, str]]
    ) -> AppStoreVersion:
        attributes = entry["attributes"]
        version = attributes["versionString"]
        if not isinstance(version, str) or not version:
            raise ValueError(f"version {entry['id']!r} has no versionString")
        relationships = entry.get("relationships") or {}
        localizations = relationships.get("appStoreVersionLocalizations", {})
        linked = localizations.get("data") or []
        return AppStoreVersion(
            version_id=str(entry["id"]),
            version=version,
            platform=str(attributes["platform"]),
            state=attributes.get("appVersionState"),
            release_type=attributes.get("releaseType"),
            created_at=self._parse_version_date(attributes.get("createdDate")),
            earliest_release_date=self._parse_version_date(
                attributes.get("earliestReleaseDate")
            ),
            release_notes=dict(
                notes[link["id"]] for link in linked if link["id"] in notes
            ),
        )

    def _parse_version_date(self, value: Any) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError(f"date {value!r} has no UTC offset")
        return parsed

    def _read_next_cursor(self, body: dict[str, Any], app_id: str) -> str | None:
        """``links.next``, only if it points back at this same endpoint.

        It is followed with the JWT attached, so a next link anywhere else is
        refused rather than handed the token.
        """
        next_cursor = (body.get("links") or {}).get("next")
        if next_cursor is None:
            return None
        if not isinstance(next_cursor, str):
            raise TypeError(
                f"'links.next' is {type(next_cursor).__name__}, expected a string"
            )
        parsed = urlsplit(next_cursor)
        if (
            parsed.scheme != "https"
            or parsed.netloc != urlsplit(self.API_BASE).netloc
            or parsed.path != self._path(app_id)
        ):
            raise ValueError(f"refusing 'links.next' outside {self._path(app_id)}")
        return next_cursor

    def _sort_newest_first(
        self, versions: list[AppStoreVersion]
    ) -> list[AppStoreVersion]:
        return sorted(
            versions,
            key=lambda version: version.created_at or _OLDEST,
            reverse=True,
        )
