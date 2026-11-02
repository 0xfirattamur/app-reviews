"""Google Play Developer API authentication via service account.

A self-signed JWT exchanged for an OAuth bearer token, on ``cryptography`` and
this package's own HTTP client, with no ``google-auth`` dependency.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from collections.abc import Mapping
from typing import Any, ClassVar

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app_reviews.core.classify import describe_failure, error_for, http_error
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient, HttpResponse
from app_reviews.core.jwt import encode_base64url, encode_jwt_segment
from app_reviews.errors import AuthError, ParseError
from app_reviews.models.config import GooglePlayAuth, ServiceAccountCredentials

REJECTED_CREDENTIAL_ERRORS = frozenset(
    {"invalid_grant", "invalid_client", "unauthorized_client"}
)
"""The OAuth ``error`` codes with which Google refuses a key, rather than a request.

Google answers these with HTTP 400, not 401, so the status alone would class a
revoked or disabled service account as a ``RequestError`` that no caller knows to
treat as configuration.
"""


def _is_rejected_credential(response: HttpResponse) -> bool:
    """Whether a 400 from the token endpoint says Google refused the credential.

    Any other 400 (``invalid_request``, an unreadable body) stays a plain request
    failure: the key may be fine and the request malformed.
    """
    if response.status != 400:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    code = body.get("error")
    return isinstance(code, str) and code in REJECTED_CREDENTIAL_ERRORS


class GoogleAuth(PooledClient):
    """Exchanges a service-account key for Google OAuth bearer tokens.

    Satisfies ``core.auth.TokenSource``, so a provider can ask per request, and
    keeps one token until it nears expiry.

    Give the key as ``service_account_path`` (the JSON file) or as
    ``service_account_info`` (the parsed JSON mapping); exactly one of the two,
    as for ``GooglePlayAuth``. The mapping is unbound before anything can raise,
    so no frame of a failure holds it.

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

    MAX_EXPIRES_IN = 3600
    """Ceiling on a token's claimed lifetime. A response is remote content: an
    absurd ``expires_in`` would otherwise pin one token, revoked or not, for the
    life of the process."""

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
        service_account_path: str | None = None,
        *,
        service_account_info: Mapping[str, Any] | None = None,
        http: HttpClient | None = None,
    ) -> None:
        ambiguous = (service_account_path is None) == (service_account_info is None)
        auth = None
        if not ambiguous:
            auth = GooglePlayAuth(
                service_account_path=service_account_path,
                service_account_info=service_account_info,
            )
        del service_account_info
        if auth is None:
            raise ValueError(
                "Pass exactly one of service_account_path or service_account_info."
            )
        self._credentials = self._load(auth)
        super().__init__(http=http)
        self._key: RSAPrivateKey | None = None
        self._bearer_header: str | None = None
        self._expires_at = 0.0

    def close(self) -> None:
        """Clear cached credentials and close only a pool created here."""
        self._key = None
        self._bearer_header = None
        self._expires_at = 0.0
        super().close()

    async def aclose(self) -> None:
        """Async equivalent of :meth:`close`."""
        self._key = None
        self._bearer_header = None
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
        url, body = self._build_token_request()
        response = self._http.post(
            url, body=body, headers=self._FORM_HEADERS, follow_redirects=False
        )
        return self._store_token(*self._read_token(response))

    async def aauthorization_header(self) -> str:
        """Async equivalent of ``authorization_header``.

        Only the exchange is awaited. Building and signing the assertion is local
        CPU work with nothing to await, so it runs off the loop rather than
        blocking it on an RSA signature.
        """
        if (cached := self._cached_header()) is not None:
            return cached
        url, body = await asyncio.to_thread(self._build_token_request)
        response = await self._http.apost(
            url, body=body, headers=self._FORM_HEADERS, follow_redirects=False
        )
        return self._store_token(*self._read_token(response))

    def _load(self, auth: GooglePlayAuth) -> ServiceAccountCredentials:
        """Read and validate the service-account JSON, as ``AuthError`` if unusable.

        The document comes from ``service_account_path`` or, already parsed,
        from ``service_account_info``. Everything here fails as a stdlib
        exception otherwise (a missing file is ``FileNotFoundError``, a truncated
        one a ``json.JSONDecodeError``), and a caller cannot be expected to catch
        those alongside the package's own errors.

        No failure path leaves key material reachable from the raised error. What
        is at stake here is larger than one field: the document is the whole
        service-account file, and an error reporter capturing frame locals (and
        walking ``__cause__``) would take all of it. So this function never binds
        the document to a local: it travels from ``_read_document`` straight into
        ``_credentials_from_document``, which unbinds it and the PEM before any
        raise. See ``tests/app_reviews/test_credential_hygiene.py``.
        """
        origin = self._describe_origin(auth)
        return self._credentials_from_document(origin, self._read_document(auth))

    @staticmethod
    def _describe_origin(auth: GooglePlayAuth) -> str:
        """Where the key came from, as the subject of an error message."""
        if auth.service_account_path is None:
            return "The Google service_account_info"
        return f"The Google service account key at {auth.service_account_path!r}"

    def _read_document(self, auth: GooglePlayAuth) -> Any:
        """The service-account document: the given mapping, or the parsed file.

        A file failure is an ``AuthError``. A decoder failure is re-raised
        *after* its ``except`` block, so no ``__cause__`` or ``__context__``
        carries the decoder's frames, which hold the document.
        """
        path = auth.service_account_path
        if path is None:
            return auth.service_account_info
        reason: str | None = None
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except OSError as exc:
            raise AuthError(
                f"Cannot read the Google service account key at {path!r}: {exc}"
            ) from exc
        except ValueError as exc:  # JSONDecodeError, UnicodeDecodeError
            reason = str(exc)
        raise AuthError(f"{self._describe_origin(auth)} is not valid JSON: {reason}")

    def _credentials_from_document(
        self, origin: str, document: Any
    ) -> ServiceAccountCredentials:
        """Validate the document's three fields into credentials.

        The failure is re-raised outside the ``except``, attaching no
        ``__cause__`` or ``__context__``: the frame that actually fails is
        ``ServiceAccountCredentials.__init__``, generated by ``dataclass``, which
        binds the key as a parameter beyond this function's reach. The reason
        text still travels; only the frames are dropped.
        """
        if not isinstance(document, Mapping):
            del document
            raise AuthError(f"{origin} is not a JSON object")

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
        except (TypeError, ValueError) as exc:
            reason = str(exc)
        del private_key_pem

        if credentials is None:
            raise AuthError(f"{origin} is unusable: {reason}")
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

    def _build_token_request(self) -> tuple[str, str]:
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
        could not narrow ``self._bearer_header`` for the caller, so both entry points
        needed a ``type: ignore`` on their return, silencing the checker on a
        public method to work around a private one's shape.

        The header is read once into a local so a concurrent ``close()`` cannot
        turn the check's ``True`` into a returned ``None``.
        """
        header = self._bearer_header
        if header is not None and time.time() < self._expires_at:
            return header
        return None

    def _store_token(self, token: str, expires_in: int) -> str:
        self._bearer_header = f"Bearer {token}"
        self._expires_at = time.time() + expires_in - self.REFRESH_MARGIN_SECONDS
        return self._bearer_header

    def _read_token(self, response: HttpResponse) -> tuple[str, int]:
        """The access token and its lifetime, or the error that explains why not.

        Each failure raises the class matching what went wrong, through the same
        ``error_for`` the search path uses: ``AuthError`` only for a rejected
        credential, ``TransportError`` for a dead connection, ``ServerError`` for
        a 5xx from Google, ``ParseError`` for a success carrying no token. The
        distinction decides whether retrying can possibly help.
        """
        self._raise_for_failed_exchange(response)
        return self._parse_token(response)

    def _raise_for_failed_exchange(self, response: HttpResponse) -> None:
        """Raise the error a transport failure or a non-2xx answer stands for.

        A transport failure sets ``status=0`` with no body, so it is handled
        before the generic non-2xx check to avoid reporting a misleading
        "HTTP 0".
        """
        failure = describe_failure(response, "Google token exchange")
        if failure is None:
            return
        if response.transport_error is not None:
            error_class = error_for(
                response.status, response.transport_error, credentialed=True
            )
            raise error_class(
                f"Google token exchange failed: {failure}",
                status=response.status or None,
            )
        # Truncated: the body is remote content and ends up in an exception
        # message that the walk logs at WARNING.
        message = (
            f"Google token exchange failed (HTTP {response.status}): "
            f"{response.body[: self.ERROR_BODY_CHARS]}"
        )
        if _is_rejected_credential(response):
            raise AuthError(message, status=response.status)
        raise http_error(response, message, credentialed=True)

    def _parse_token(self, response: HttpResponse) -> tuple[str, int]:
        """The access token and its lifetime from a 2xx body, or ``ParseError``."""
        try:
            body = response.json()
            token = body["access_token"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ParseError(
                f"Google token exchange returned no access_token: {exc}",
                status=response.status,
            ) from exc
        if not isinstance(token, str) or not token:
            raise ParseError(
                "Google token exchange returned an access_token that is not a "
                "non-empty string",
                status=response.status,
            )
        return token, self._expires_in(body)

    def _expires_in(self, body: dict[str, Any]) -> int:
        """The token's lifetime in seconds, defaulted when Google does not say.

        Anything unusable falls back rather than raising: the exchange already
        succeeded and a token is in hand, so all this decides is when to
        re-exchange. A non-positive lifetime would mean re-exchanging on every
        single request, and one beyond ``MAX_EXPIRES_IN`` is clamped. ``OverflowError``
        is caught because JSON's ``1e999`` parses to ``inf``.
        """
        try:
            seconds = int(body.get("expires_in", self.DEFAULT_EXPIRES_IN))
        except (TypeError, ValueError, OverflowError):
            return self.DEFAULT_EXPIRES_IN
        if seconds <= 0:
            return self.DEFAULT_EXPIRES_IN
        return min(seconds, self.MAX_EXPIRES_IN)
