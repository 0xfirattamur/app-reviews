"""The pooled-client base: what every client that owns a connection pool shares.

Separate from ``core.http`` because it is not transport. ``http`` performs
requests; this decides that a client has one pool, borrows one when handed an
``HttpClient``, and closes it. Keeping it here means ``appstore.search`` no
longer imports its base class from a module named "http".
"""

from __future__ import annotations

from typing import Self

from app_reviews.core.http import HttpClient
from app_reviews.models.config import RetryConfig

__all__ = ["PooledClient"]


class PooledClient:
    """Shared plumbing for the clients that own a connection pool.

    Deliberately not an interface: it declares nothing abstract, and nothing
    dispatches on it. It exists so that building the pool from ``proxy`` and
    ``retry`` (and closing it again) is written once instead of in every
    client. The *interfaces* in this package are Protocols (``ReviewProvider``,
    ``SearchClient``); this is only the behaviour they'd otherwise duplicate.
    """

    def __init__(
        self,
        *,
        proxy: str | None = None,
        retry: RetryConfig | None = None,
        http: HttpClient | None = None,
    ) -> None:
        """Build a client that owns, or borrows, one connection pool.

        ``proxy`` and ``retry`` configure a pool built here. Pass ``http`` to
        supply the pool yourself (to share one between clients, or to inject a
        transport in tests), in which case those settings already belong to it,
        so passing them alongside ``http`` raises rather than being dropped.
        """
        if http is not None:
            conflicting = [
                name
                for name, value in (("proxy", proxy), ("retry", retry))
                if value is not None
            ]
            if conflicting:
                raise TypeError(
                    f"pass either http= or {'/'.join(conflicting)}=, not both: "
                    f"{', '.join(conflicting)} would be ignored, because http= "
                    f"already carries its own settings. Configure the HttpClient "
                    f"you pass in instead."
                )
        self._retry = retry or RetryConfig()
        self._owns_http = http is None
        self._http = http or HttpClient(
            timeout=self._retry.timeout, proxy=proxy, retry=self._retry
        )

    def close(self) -> None:
        """Release this client's pooled connections. Safe to call repeatedly.

        Pooling keeps sockets open between requests, so a long-lived client
        should be closed, or used as a context manager, rather than left to
        the garbage collector. The client stays usable: the next request
        reopens the pool.
        """
        if self._owns_http:
            self._http.close()

    async def aclose(self) -> None:
        """Async twin of ``close``, for connections the async ladder opened."""
        if self._owns_http:
            await self._http.aclose()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()
