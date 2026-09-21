"""The search client interface, and the one step both implementations share."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from app_reviews.models.country import Country

if TYPE_CHECKING:
    from collections.abc import Callable

    from app_reviews.core.http import HttpClient, HttpResponse
    from app_reviews.models.metadata import AppMetadata

_Parsed = TypeVar("_Parsed")


class SearchClient(Protocol):
    """What a store search client does.

    Structural, so nothing inherits it. This was an ABC whose four abstract
    methods hid three field assignments: all interface, no behaviour, which is
    precisely what a Protocol is for. Being structural also removed the reason
    ``lookup`` had to reach into a private ``_transport`` field: the pool is now
    a constructor argument of the concrete clients rather than a hole punched
    into a shared base.

    The behaviour that really is shared lives elsewhere: the connection pool and
    its lifecycle in ``PooledClient``, and the request-then-parse step in
    ``get_and_parse`` below.
    """

    def search(
        self,
        query: str,
        *,
        country: Country = Country.US,
        limit: int = 50,
    ) -> list[AppMetadata]:
        """Search the store, newest-relevance first, capped at ``limit``."""
        ...

    async def asearch(
        self,
        query: str,
        *,
        country: Country = Country.US,
        limit: int = 50,
    ) -> list[AppMetadata]:
        """Async equivalent of ``search``."""
        ...

    def lookup(
        self,
        app_id: str,
        *,
        country: Country = Country.US,
    ) -> AppMetadata | None:
        """Fetch one app's metadata, or None if the store has no such app."""
        ...

    async def alookup(
        self,
        app_id: str,
        *,
        country: Country = Country.US,
    ) -> AppMetadata | None:
        """Async equivalent of ``lookup``."""
        ...


def get_and_parse(
    http: HttpClient,
    url: str,
    params: dict[str, str],
    parse: Callable[[HttpResponse], _Parsed],
    *,
    headers: dict[str, str] | None = None,
) -> _Parsed:
    """GET ``url`` with ``params``, then hand the response to ``parse``.

    Each search client's search/asearch/lookup/alookup quartet was the same
    three steps (build params, GET, parse) written out eight times across two
    modules. This is that step, once, with no base class to inherit it from.
    """
    return parse(http.get(url, params=params, headers=headers))


async def aget_and_parse(
    http: HttpClient,
    url: str,
    params: dict[str, str],
    parse: Callable[[HttpResponse], _Parsed],
    *,
    headers: dict[str, str] | None = None,
) -> _Parsed:
    """Async equivalent of ``get_and_parse``."""
    return parse(await http.aget(url, params=params, headers=headers))


def scraped_text(value: Any) -> str | None:
    """Trimmed text from an untyped payload, or None if it is unusable.

    Both search clients fill ``AppMetadata`` fields declared ``str`` from values
    that arrive as JSON or as a scraped array, where the same key can be absent,
    explicitly ``null``, or a list. ``dict.get(key, default)`` only falls back
    when the key is *absent*, so a present ``null`` reached those fields before
    this existed.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return str(value)
    return (value.strip() or None) if isinstance(value, str) else None


def scraped_datetime(value: Any) -> datetime | None:
    """An ISO 8601 timestamp from an untyped payload, or None if it is unusable.

    Returns None rather than a default, unlike ``scraped_text`` and
    ``scraped_number``: an absent date has no stand-in that would not be mistaken
    for a real one downstream.

    Always tz-aware. A naive datetime raises ``TypeError`` the moment it is
    compared against an aware one, so a value iTunes sent without a zone is read
    as UTC, which is what it means by ``Z``.
    """
    if not isinstance(value, str) or not (text := value.strip()):
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def scraped_number(value: Any, default: float) -> float:
    """A float from an untyped payload, or ``default`` if it is unusable.

    A rating that arrives as ``"four-point-five"`` costs the field, not the call:
    ``float()`` on it raises, and neither search method has a way to report one
    bad field on an otherwise usable result.
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default
