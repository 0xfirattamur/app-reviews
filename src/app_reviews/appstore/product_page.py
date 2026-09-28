"""Reads the version history embedded in the public App Store product page.

Scraped, undocumented page structure: best-effort, and may break when Apple
changes the page.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from app_reviews.core.classify import raise_for_http_failure
from app_reviews.core.http import HttpResponse
from app_reviews.core.search import scraped_text
from app_reviews.errors import ParseError
from app_reviews.models.metadata import AppVersionEntry

__all__ = ["parse_version_history"]

_API = "App Store product page"

_SERVER_DATA = re.compile(
    r'<script[^>]*\bid="serialized-server-data"[^>]*>(.*?)</script>', re.DOTALL
)
"""The JSON the page embeds to hydrate itself."""

_VERSION_LABEL = re.compile(r"^Version(?:\s+|$)")
"""The prefix some storefronts render before the number: ``Version 9.1.84``."""


def parse_version_history(response: HttpResponse) -> list[AppVersionEntry]:
    """The page's version history, newest first.

    ``[]`` for no such app (404) or a page without a history. Raises
    ``ParseError`` for page data or an entry it cannot read, since a partial
    answer would pass for a real history.
    """
    if response.status == 404:
        return []
    raise_for_http_failure(response, _API, credentialed=False)
    status = response.status
    match = _SERVER_DATA.search(response.body)
    if match is None:
        raise ParseError(f"{_API} carried no embedded page data", status=status)
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ParseError(
            f"Unreadable page data from {_API}: {exc}", status=status
        ) from exc

    action = _version_history_action(data)
    if action is None:
        return []
    entries = [_entry(item, status) for item in _items(action["pageData"], status)]
    entries.sort(key=lambda entry: entry.released_at, reverse=True)
    return entries


def _version_history_action(data: Any) -> dict[str, Any] | None:
    """The action that opens "Version History", found by content, not by path."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("page") == "versionHistory" and "pageData" in node:
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _items(page_data: Any, status: int) -> list[Any]:
    shelves = page_data.get("shelves") if isinstance(page_data, dict) else None
    if not isinstance(shelves, list):
        raise ParseError(
            "App Store version history has no list of shelves", status=status
        )
    items: list[Any] = []
    for shelf in shelves:
        shelf_items = shelf.get("items") if isinstance(shelf, dict) else None
        if not isinstance(shelf_items, list):
            raise ParseError(
                "App Store version history has a shelf without items", status=status
            )
        items.extend(shelf_items)
    return items


def _entry(item: Any, status: int) -> AppVersionEntry:
    if not isinstance(item, dict):
        raise ParseError(
            f"App Store version history entry is {type(item).__name__}, "
            "expected an object",
            status=status,
        )
    label = scraped_text(item.get("primarySubtitle"))
    version = _VERSION_LABEL.sub("", label) if label else None
    if not version:
        raise ParseError(
            "App Store version history entry has no version", status=status
        )
    released_at = _released_at(scraped_text(item.get("secondarySubtitle")))
    if released_at is None:
        raise ParseError(
            f"App Store version history entry {version} has no readable date",
            status=status,
        )
    return AppVersionEntry(
        version=version,
        released_at=released_at,
        release_notes=scraped_text(item.get("text")),
    )


def _released_at(text: str | None) -> datetime | None:
    """``Fri Sep 18 2026 06:25:38 GMT+0000 (Coordinated Universal Time)`` in UTC."""
    if text is None:
        return None
    try:
        parsed = datetime.strptime(text.split(" (", 1)[0], "%a %b %d %Y %H:%M:%S GMT%z")
    except ValueError:
        return None
    return parsed.astimezone(UTC)
