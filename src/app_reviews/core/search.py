"""The search client interface both store implementations satisfy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from app_reviews.models.country import Country

if TYPE_CHECKING:
    from app_reviews.models.metadata import AppMetadata


class SearchClient(Protocol):
    """What a store search client does.

    Structural, so nothing inherits it. The behaviour that really is shared
    lives elsewhere: the connection pool and its lifecycle in ``PooledClient``.
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
