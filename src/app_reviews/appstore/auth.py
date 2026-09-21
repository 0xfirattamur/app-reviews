"""App Store Connect JWT authentication."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Self

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from app_reviews.core.jwt import (
    convert_der_to_raw_signature,
    encode_base64url,
    encode_jwt_segment,
    load_ec_private_key_from_pem,
)
from app_reviews.errors import AuthError
from app_reviews.models.config import ConnectCredentials

TOKEN_EXPIRY_SECONDS = 20 * 60
"""Apple's cap. A Connect token minted for longer than 20 minutes is rejected."""

REFRESH_MARGIN_SECONDS = 60
"""Re-sign this long before expiry, so a request that starts just under the wire
does not arrive with a token that has already died."""


class ConnectAuth:
    """Signs ES256 JWTs for App Store Connect, and keeps one until it nears expiry.

    Satisfies ``core.auth.TokenSource``, so a provider can ask per request.
    """

    def __init__(self, credentials: ConnectCredentials) -> None:
        self._credentials = credentials
        self._key: ec.EllipticCurvePrivateKey | None = None
        self._token: str | None = None
        self._expires_at = 0.0

    def _build_header(self) -> dict[str, str]:
        return {"alg": "ES256", "kid": self._credentials.key_id, "typ": "JWT"}

    def _build_payload(self, issued_at: int) -> dict[str, Any]:
        return {
            "iss": self._credentials.issuer_id,
            "iat": issued_at,
            "exp": issued_at + TOKEN_EXPIRY_SECONDS,
            "aud": "appstoreconnect-v1",
        }

    def _load_key(self) -> ec.EllipticCurvePrivateKey:
        """Parse the PEM once; signing happens per token.

        ``ConnectCredentials`` only checks the PEM markers, so a key that is
        well-formed on the outside but unusable inside fails here, at the first
        signature, which is during a request. ``TypeError`` is caught alongside
        ``ValueError`` because a readable key of the wrong algorithm raises that
        one: an RSA key saved as a .p8 parses fine and only then turns out not to
        be EC. Both arrive as ``AuthError`` rather than as a stdlib exception.
        """
        if self._key is None:
            try:
                self._key = load_ec_private_key_from_pem(self._credentials.private_key)
            except (TypeError, ValueError) as exc:
                raise AuthError(
                    f"The App Store Connect key for {self._credentials.key_id} "
                    f"could not be loaded: {exc}"
                ) from exc
        return self._key

    def _sign(self, message: bytes) -> bytes:
        """Sign with the EC private key, returning raw ``(r || s)`` bytes."""
        key = self._load_key()
        return convert_der_to_raw_signature(
            key.sign(message, ec.ECDSA(hashes.SHA256())), key.key_size
        )

    def generate_token(self, issued_at: int | None = None) -> str:
        """A freshly signed JWT, as ``<header>.<payload>.<signature>``."""
        now = int(time.time()) if issued_at is None else issued_at
        header_b64 = encode_jwt_segment(self._build_header())
        payload_b64 = encode_jwt_segment(self._build_payload(now))

        signing_input = f"{header_b64}.{payload_b64}".encode()
        signature_b64 = encode_base64url(self._sign(signing_input))

        return f"{header_b64}.{payload_b64}.{signature_b64}"

    def authorization_header(self) -> str:
        """A ``Bearer`` value, re-signed once the held token nears its expiry.

        The clock is read once and used for both the JWT's ``iat``/``exp`` and the
        local expiry, so what this class believes about the token cannot drift
        from what it actually signed.
        """
        if self._token is None or time.time() >= self._expires_at:
            issued_at = int(time.time())
            self._token = self.generate_token(issued_at)
            self._expires_at = issued_at + TOKEN_EXPIRY_SECONDS - REFRESH_MARGIN_SECONDS
        return f"Bearer {self._token}"

    async def aauthorization_header(self) -> str:
        """Signing is local CPU work with nothing to await, so it runs off the loop."""
        return await asyncio.to_thread(self.authorization_header)

    def close(self) -> None:
        """Discard cached signing material and bearer-token state."""
        self._key = None
        self._token = None
        self._expires_at = 0.0

    async def aclose(self) -> None:
        """Async lifecycle twin; cleanup itself performs no I/O."""
        self.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()
