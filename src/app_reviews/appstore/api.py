"""The base for clients that call App Store Connect with an ``AppStoreAuth``."""

from __future__ import annotations

from urllib.parse import urlsplit

from app_reviews.appstore.auth import ConnectAuth, load_connect_credentials
from app_reviews.core.auth import LazyTokenSourceClient
from app_reviews.core.http import HttpClient
from app_reviews.core.ratelimit import RequestLimiter
from app_reviews.models.config import AppStoreAuth, RetryConfig


class ConnectAPIClient(LazyTokenSourceClient):
    """Owns a pool and a lazily built ``ConnectAuth`` for one ``AppStoreAuth``.

    The key is loaded at the first request rather than in ``__init__``, so a
    ``key_path`` is read off the event loop on the async path, and a bad key
    surfaces as ``AuthError`` from the call that needed it, as it does for
    ``AppStoreReviews``.
    """

    API_BASE = "https://api.appstoreconnect.apple.com"

    def __init__(
        self,
        auth: AppStoreAuth,
        *,
        http: HttpClient | None = None,
        proxy: str | None = None,
        retry: RetryConfig | None = None,
        rate_limiter: RequestLimiter | None = None,
    ) -> None:
        super().__init__(
            self._build_connect_auth,
            proxy=proxy,
            retry=retry,
            http=http,
            rate_limiter=rate_limiter,
        )
        self._auth = auth

    def _build_connect_auth(self) -> ConnectAuth:
        return ConnectAuth(load_connect_credentials(self._auth))


CONNECT_HOST = "api.appstoreconnect.apple.com"
"""The one host a Connect request, and so a ``links.next`` cursor, may name."""


def refuse_foreign_url(url: str, *, host: str, exact_path: str) -> str | None:
    """Why ``url`` may not be requested with a Connect JWT, or None if it may.

    Connect hands back whole URLs in ``links.next``, and callers persist them, so
    one is untrusted input by the time it returns. The request that follows
    carries the signed JWT: a URL on another host, port or path would hand that
    token to whoever owns it, or aim it at another endpoint the key can reach.

    ``urlsplit`` raises on a malformed IPv6 literal, so even parsing is guarded:
    this exists to *report* a bad URL, and raising out of it defeats the point.

    Control characters are refused before anything else: ``urlsplit`` strips
    them, so a URL could pass every other check and still forge a record in a
    log line that renders it.
    """
    if any(character in url for character in "\r\n\t"):
        return "it contains control characters"
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        return f"it cannot be parsed as a URL ({exc})"
    if parsed.scheme != "https":
        return "expected an https URL"
    if parsed.username is not None or parsed.password is not None:
        return "user-info is not allowed"
    if parsed.hostname != host:
        return f"expected a URL on {host}"
    try:
        port = parsed.port
    except ValueError as exc:
        return f"it has an invalid port ({exc})"
    if port not in (None, 443):
        return f"expected the default port, got {port}"
    if parsed.path != exact_path:
        return f"expected the exact path {exact_path}"
    if parsed.fragment:
        return "fragments are not allowed"
    return None
