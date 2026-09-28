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
            page, url = self._page(response, app_id)
            found += page
        return self._newest_first(found)

    async def aversions(self, app_id: str) -> list[AppStoreVersion]:
        """Async equivalent of ``versions``."""
        found: list[AppStoreVersion] = []
        url: str | None = self._first_url(app_id)
        seen: set[str] = set()
        while url is not None and url not in seen:
            seen.add(url)
            response = await self._http.aget(url, headers=await self._aheaders())
            page, url = self._page(response, app_id)
            found += page
        return self._newest_first(found)

    def _first_url(self, app_id: str) -> str:
        """Escaped: the id lands in the path of a request carrying the JWT."""
        return f"{self.API_BASE}{self._path(app_id)}?{_QUERY}"

    def _path(self, app_id: str) -> str:
        return f"/v1/apps/{quote(app_id, safe='')}/appStoreVersions"

    def _page(
        self, response: HttpResponse, app_id: str
    ) -> tuple[list[AppStoreVersion], str | None]:
        raise_for_http_failure(response, _API)
        try:
            body = response.json()
            notes = self._notes_by_localization(body.get("included") or [])
            versions = [self._version(entry, notes) for entry in body["data"]]
            next_url = self._next_url(body, app_id)
        except _UNUSABLE as exc:
            raise ParseError(
                f"Malformed appStoreVersions page from {_API}: {exc}",
                status=response.status,
            ) from exc
        return versions, next_url

    def _notes_by_localization(self, included: list[Any]) -> dict[str, tuple[str, str]]:
        """``{localization id: (locale, whatsNew)}`` for localizations with text."""
        notes: dict[str, tuple[str, str]] = {}
        for item in included:
            if item.get("type") != "appStoreVersionLocalizations":
                continue
            attrs = item.get("attributes") or {}
            text = attrs.get("whatsNew")
            if isinstance(text, str) and text.strip():
                notes[item["id"]] = (str(attrs["locale"]), text.strip())
        return notes

    def _version(
        self, entry: Any, notes: dict[str, tuple[str, str]]
    ) -> AppStoreVersion:
        attrs = entry["attributes"]
        version = attrs["versionString"]
        if not isinstance(version, str) or not version:
            raise ValueError(f"version {entry['id']!r} has no versionString")
        linked = (entry.get("relationships") or {}).get(
            "appStoreVersionLocalizations", {}
        ).get("data") or []
        return AppStoreVersion(
            version_id=str(entry["id"]),
            version=version,
            platform=str(attrs["platform"]),
            state=attrs.get("appVersionState"),
            release_type=attrs.get("releaseType"),
            created_at=self._date(attrs.get("createdDate")),
            earliest_release_date=self._date(attrs.get("earliestReleaseDate")),
            release_notes=dict(
                notes[link["id"]] for link in linked if link["id"] in notes
            ),
        )

    def _date(self, value: Any) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError(f"date {value!r} has no UTC offset")
        return parsed

    def _next_url(self, body: dict[str, Any], app_id: str) -> str | None:
        """``links.next``, only if it points back at this same endpoint.

        It is followed with the JWT attached, so a next link anywhere else is
        refused rather than handed the token.
        """
        nxt = (body.get("links") or {}).get("next")
        if nxt is None:
            return None
        if not isinstance(nxt, str):
            raise TypeError(f"'links.next' is {type(nxt).__name__}, expected a string")
        parsed = urlsplit(nxt)
        if (
            parsed.scheme != "https"
            or parsed.netloc != urlsplit(self.API_BASE).netloc
            or parsed.path != self._path(app_id)
        ):
            raise ValueError(f"refusing 'links.next' outside {self._path(app_id)}")
        return nxt

    def _newest_first(self, versions: list[AppStoreVersion]) -> list[AppStoreVersion]:
        return sorted(versions, key=lambda v: v.created_at or _OLDEST, reverse=True)
