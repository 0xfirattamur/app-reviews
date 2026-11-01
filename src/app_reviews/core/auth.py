"""What a provider needs from a credential, and the parts every credential shares."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Protocol

from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient
from app_reviews.core.ratelimit import RequestLimiter
from app_reviews.models.config import RetryConfig

REFRESH_MARGIN_SECONDS = 60
"""Renew a token this long before it expires, so a request that starts just under
the wire does not arrive with a token that has already died."""


class TokenSource(Protocol):
    """Supplies an ``Authorization`` header value, refreshing it as it expires.

    A provider asks per request rather than holding a string, because tokens
    expire: a Connect JWT lasts 20 minutes and a Google access token about an
    hour, both shorter than a client's useful life. Caching belongs to the
    implementation, which is the only side that knows the expiry.
    """

    def authorization_header(self) -> str: ...

    async def aauthorization_header(self) -> str: ...


class ClosableTokenSource(TokenSource, Protocol):
    """A ``TokenSource`` that holds secrets or a pool and must be released."""

    def close(self) -> None: ...

    async def aclose(self) -> None: ...


class BearerCache:
    """One ``Authorization`` header, reused until it is within the refresh margin.

    ``get`` and ``store`` take the clock as an argument so a caller that also
    signs a token with that instant cannot disagree with the cache about when it
    was issued.
    """

    def __init__(self) -> None:
        self._header: str | None = None
        self._expires_at: float = 0.0

    def get(self, now: float) -> str | None:
        """The held header while it is still worth reusing, else None.

        Returns the value rather than answering yes/no, and reads it once into a
        local: a ``clear()`` from another thread between the check and the return
        would otherwise hand back ``None`` or ``"Bearer None"``.
        """
        header = self._header
        if header is not None and now < self._expires_at:
            return header
        return None

    def store(self, token: str, lifetime: float, now: float) -> str:
        """Hold ``token`` for ``lifetime`` less the margin; return its header."""
        header = f"Bearer {token}"
        self._header = header
        self._expires_at = now + lifetime - REFRESH_MARGIN_SECONDS
        return header

    def clear(self) -> None:
        """Forget the held header, so the next ``get`` returns None."""
        self._header = None
        self._expires_at = 0.0


class LazyTokenSourceClient(PooledClient):
    """A pooled client that builds its token source at the first request.

    The source is built then rather than in ``__init__``, so a key file is read
    off the event loop on the async path, and a bad key surfaces as ``AuthError``
    from the call that needed it. ``build_token_source`` runs at most once, under
    a lock, and the result is closed with the client.
    """

    def __init__(
        self,
        build_token_source: Callable[[], ClosableTokenSource],
        *,
        http: HttpClient | None = None,
        proxy: str | None = None,
        retry: RetryConfig | None = None,
        rate_limiter: RequestLimiter | None = None,
    ) -> None:
        super().__init__(proxy=proxy, retry=retry, http=http, rate_limiter=rate_limiter)
        self._build_token_source: Callable[[], ClosableTokenSource] = build_token_source
        self._token_source: ClosableTokenSource | None = None
        self._token_source_lock = threading.Lock()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._ensure_token_source().authorization_header()}

    async def _aheaders(self) -> dict[str, str]:
        token_source = self._token_source
        if token_source is None:
            token_source = await asyncio.to_thread(self._ensure_token_source)
        return {"Authorization": await token_source.aauthorization_header()}

    def _ensure_token_source(self) -> ClosableTokenSource:
        """The token source, built by the first call and reused by every later one.

        Checked once without the lock, so a built source costs no locking, and
        again under it, so two first requests cannot both build one.
        """
        token_source = self._token_source
        if token_source is not None:
            return token_source
        with self._token_source_lock:
            if self._token_source is None:
                self._token_source = self._build_token_source()
            return self._token_source

    def close(self) -> None:
        """Release the token source and close only a pool created here."""
        if self._token_source is not None:
            self._token_source.close()
        super().close()

    async def aclose(self) -> None:
        """Async equivalent of :meth:`close`."""
        if self._token_source is not None:
            await self._token_source.aclose()
        await super().aclose()
