"""Google Play Developer API authentication via service account.

A self-signed JWT exchanged for an OAuth bearer token, on ``cryptography`` and
this package's own HTTP client, with no ``google-auth`` dependency.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from typing import Any, ClassVar

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app_reviews.core.classify import error_for
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient, HttpResponse
from app_reviews.core.jwt import encode_base64url, encode_jwt_segment
from app_reviews.errors import AuthError, ParseError
from app_reviews.models.config import ServiceAccountCredentials


class GoogleAuth(PooledClient):
    """Exchanges a service-account key for Google OAuth bearer tokens.

    Satisfies ``core.auth.TokenSource``, so a provider can ask per request, and
    keeps one token until it nears expiry.

    ``http`` is the pooled client the exchange runs on. Callers pass their own so
    it inherits the proxy and retry policy they configured; otherwise a proxied
    client's token exchange would bypass the proxy it was set up with.
    """

    SCOPE = "https://www.googleapis.com/auth/androidpublisher"
    DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
    GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"

    ASSERTION_LIFETIME_SECONDS = 3600
    """Google's cap on a self-signed assertion."""

    DEFAULT_EXPIRES_IN = 3600
    """Used when Google omits ``expires_in``. Its tokens are an hour in practice."""

    ERROR_BODY_CHARS = 200
    """How much of a failed exchange's body reaches the exception message."""

    REFRESH_MARGIN_SECONDS = 60
    """Re-exchange this long before expiry, so a request that starts just under
    the wire does not arrive with a token that has already expired."""

    _FORM_HEADERS: ClassVar[dict[str, str]] = {
        "Content-Type": "application/x-www-form-urlencoded"
    }

    def __init__(
        self,
        service_account_path: str,
        *,
        http: HttpClient | None = None,
    ) -> None:
        self._credentials = self._load(service_account_path)
        super().__init__(http=http)
        self._key: RSAPrivateKey | None = None
        self._header: str | None = None
        self._expires_at = 0.0

    def close(self) -> None:
        """Clear cached credentials and close only a pool created here."""
        self._key = None
        self._header = None
        self._expires_at = 0.0
        super().close()

    async def aclose(self) -> None:
        """Async equivalent of :meth:`close`."""
        self._key = None
        self._header = None
        self._expires_at = 0.0
        await super().aclose()

    def authorization_header(self) -> str:
        """A ``Bearer`` value, re-exchanged once the held token nears its expiry.

        Raises ``AuthError`` when the credentials were rejected,
        ``TransportError``/``ServerError`` when the attempt is worth retrying, and
        ``ParseError`` when a success carried no token. All of them
        ``AppReviewsError``.
        """
        if (cached := self._cached_header()) is not None:
            return cached
        url, body = self._token_request()
        response = self._http.post(
            url, body=body, headers=self._FORM_HEADERS, follow_redirects=False
        )
        return self._store(*self._read_token(response))

    async def aauthorization_header(self) -> str:
        """Async equivalent of ``authorization_header``.

        Only the exchange is awaited. Building and signing the assertion is local
        CPU work with nothing to await, so it runs off the loop rather than
        blocking it on an RSA signature.
        """
        if (cached := self._cached_header()) is not None:
            return cached
        url, body = await asyncio.to_thread(self._token_request)
        response = await self._http.apost(
            url, body=body, headers=self._FORM_HEADERS, follow_redirects=False
        )
        return self._store(*self._read_token(response))

    def _load(self, path: str) -> ServiceAccountCredentials:
        """Read and validate the service-account JSON, as ``AuthError`` if unusable.

        Everything here fails as a stdlib exception otherwise (a missing file is
        ``FileNotFoundError``, a truncated one a ``json.JSONDecodeError``), and a
        caller cannot be expected to catch those alongside the package's own
        errors.

        No failure path leaves key material reachable from the raised error. What
        is at stake here is larger than one field: ``document`` is the whole
        service-account file, and an error reporter capturing frame locals (and
        walking ``__cause__``) would take all of it. So the document and the PEM
        are unbound before the raise, and the validation failure is re-raised
        *after* the ``except`` block rather than inside it, attaching no
        ``__cause__`` or ``__context__``: the frame that actually fails is
        ``ServiceAccountCredentials.__init__``, generated by ``dataclass``, which
        binds the key as a parameter beyond this function's reach. The reason text
        still travels; only the frames are dropped. See
        ``tests/app_reviews/test_credential_hygiene.py``.
        """
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
        except OSError as exc:
            raise AuthError(
                f"Cannot read the Google service account key at {path!r}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise AuthError(
                f"The Google service account key at {path!r} is not valid JSON: {exc}"
            ) from exc

        if not isinstance(document, dict):
            raise AuthError(
                f"The Google service account key at {path!r} is not a JSON object"
            )

        client_email = document.get("client_email", "")
        private_key_pem = document.get("private_key", "")
        token_uri = document.get("token_uri") or self.DEFAULT_TOKEN_URI
        del document

        credentials: ServiceAccountCredentials | None = None
        reason: str | None = None
        try:
            credentials = ServiceAccountCredentials(
                client_email=client_email,
                private_key_pem=private_key_pem,
                token_uri=token_uri,
            )
        except ValueError as exc:
            reason = str(exc)
        del private_key_pem

        if credentials is None:
            raise AuthError(
                f"The Google service account key at {path!r} is unusable: {reason}"
            )
        return credentials

    def _load_key(self) -> RSAPrivateKey:
        """Parse the PEM once; signing happens per assertion.

        ``ServiceAccountCredentials`` only checks the PEM markers, so a key that
        is well-formed on the outside but unreadable inside fails here, at the
        first signature, which is during a request. Reported as ``AuthError`` so
        it does not arrive as a ``cryptography`` ``ValueError``.
        """
        if self._key is None:
            try:
                key = serialization.load_pem_private_key(
                    self._credentials.private_key_pem.encode(), password=None
                )
            except (TypeError, ValueError) as exc:
                raise AuthError(
                    f"The Google service account key for "
                    f"{self._credentials.client_email} could not be loaded: {exc}"
                ) from exc
            if not isinstance(key, RSAPrivateKey):
                raise AuthError(
                    f"The Google service account key for "
                    f"{self._credentials.client_email} is a "
                    f"{type(key).__name__}, but Google signs these assertions "
                    f"with RSA"
                )
            self._key = key
        return self._key

    def _build_jwt(self) -> str:
        """A signed assertion, as ``<header>.<payload>.<signature>``.

        ``aud`` is the token endpoint, which binds the assertion to the host it
        is about to be sent to.
        """
        now = int(time.time())
        header_b64 = encode_jwt_segment({"alg": "RS256", "typ": "JWT"})
        payload_b64 = encode_jwt_segment(
            {
                "iss": self._credentials.client_email,
                "scope": self.SCOPE,
                "aud": self._credentials.token_uri,
                "iat": now,
                "exp": now + self.ASSERTION_LIFETIME_SECONDS,
            }
        )

        signing_input = f"{header_b64}.{payload_b64}".encode()
        signature = self._load_key().sign(
            signing_input, padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{header_b64}.{payload_b64}.{encode_base64url(signature)}"

    def _token_request(self) -> tuple[str, str]:
        """The ``(url, urlencoded_body)`` pair for the exchange.

        Shared by the sync and async paths so both send an identical request.
        """
        body = urllib.parse.urlencode(
            {"grant_type": self.GRANT_TYPE, "assertion": self._build_jwt()}
        )
        return self._credentials.token_uri, body

    def _cached_header(self) -> str | None:
        """The held token while it is still worth reusing, else None.

        Returns the value rather than answering yes/no. A boolean ``_fresh()``
        could not narrow ``self._header`` for the caller, so both entry points
        needed a ``type: ignore`` on their return, silencing the checker on a
        public method to work around a private one's shape.
        """
        if self._header is not None and time.time() < self._expires_at:
            return self._header
        return None

    def _store(self, token: str, expires_in: int) -> str:
        self._header = f"Bearer {token}"
        self._expires_at = time.time() + expires_in - self.REFRESH_MARGIN_SECONDS
        return self._header

    def _read_token(self, response: HttpResponse) -> tuple[str, int]:
        """The access token and its lifetime, or the error that explains why not.

        A transport failure sets ``status=0`` with no body, so it is handled
        before the generic non-2xx check to avoid reporting a misleading
        "HTTP 0".

        Each failure raises the class matching what went wrong, through the same
        ``error_for`` the search path uses: ``AuthError`` only for a rejected
        credential, ``TransportError`` for a dead connection, ``ServerError`` for
        a 5xx from Google, ``ParseError`` for a success carrying no token. The
        distinction decides whether retrying can possibly help.
        """
        if response.transport_error is not None:
            raise error_for(response.status, response.transport_error)(
                f"Google token exchange failed: {response.transport_error}",
                status=response.status or None,
            )
        if not response.ok:
            # Truncated: the body is remote content and ends up in an exception
            # message that the walk logs at WARNING.
            raise error_for(response.status)(
                f"Google token exchange failed (HTTP {response.status}): "
                f"{response.body[: self.ERROR_BODY_CHARS]}",
                status=response.status,
            )

        try:
            body = response.json()
            token = body["access_token"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ParseError(
                f"Google token exchange returned no access_token: {exc}",
                status=response.status,
            ) from exc
        return str(token), self._expires_in(body)

    def _expires_in(self, body: dict[str, Any]) -> int:
        """The token's lifetime in seconds, defaulted when Google does not say.

        Anything unusable falls back rather than raising: the exchange already
        succeeded and a token is in hand, so all this decides is when to
        re-exchange. A non-positive lifetime would mean re-exchanging on every
        single request.
        """
        try:
            seconds = int(body.get("expires_in", self.DEFAULT_EXPIRES_IN))
        except (TypeError, ValueError):
            return self.DEFAULT_EXPIRES_IN
        return seconds if seconds > 0 else self.DEFAULT_EXPIRES_IN
