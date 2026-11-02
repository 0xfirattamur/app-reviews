"""Tests for App Store Connect authentication."""

import pytest

from app_reviews.appstore.auth import ConnectAuth
from app_reviews.models.config import ConnectCredentials
from tests.app_reviews.factories import connect_credentials


class TestConnectCredentials:
    def test_missing_key_id_raises(self) -> None:
        with pytest.raises(ValueError, match="key_id"):
            connect_credentials(key_id="")

    def test_missing_issuer_id_raises(self) -> None:
        with pytest.raises(ValueError, match="issuer_id"):
            connect_credentials(issuer_id="")

    def test_missing_private_key_raises(self) -> None:
        with pytest.raises(ValueError, match="private_key"):
            connect_credentials(private_key="")

    def test_non_pem_private_key_raises(self) -> None:
        with pytest.raises(ValueError, match="PEM"):
            connect_credentials(private_key="not-a-pem-key")

    def test_valid_credentials(self) -> None:
        creds = connect_credentials()
        assert creds.key_id == "KEY123"
        assert creds.issuer_id == "ISSUER456"


class TestConnectAuth:
    def test_authorization_header_has_bearer_prefix(self) -> None:
        auth = ConnectAuth(connect_credentials())
        header = auth.authorization_header()
        assert header.startswith("Bearer ")
        token = header.removeprefix("Bearer ")
        assert len(token.split(".")) == 3

    def test_generate_token_returns_jwt_string(self) -> None:
        auth = ConnectAuth(connect_credentials())
        token = auth.generate_token()
        assert isinstance(token, str)
        assert len(token.split(".")) == 3


class TestTheAssertionIsSignedOverHeaderAndPayload:
    """Signing only the payload yields a well-formed token that every request
    then fails on. Nothing verified the signature against the public key."""

    def test_the_jwt_verifies_end_to_end(self):
        import base64

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        from app_reviews.appstore.auth import ConnectAuth

        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

        token = ConnectAuth(
            ConnectCredentials(key_id="K", issuer_id="I", private_key=pem)
        ).generate_token()
        header_b64, payload_b64, signature_b64 = token.split(".")

        raw = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
        half = len(raw) // 2
        der = utils.encode_dss_signature(
            int.from_bytes(raw[:half], "big"), int.from_bytes(raw[half:], "big")
        )

        # Verifies only if the signing input really was `header.payload`.
        key.public_key().verify(
            der, f"{header_b64}.{payload_b64}".encode(), ec.ECDSA(hashes.SHA256())
        )


class TestTheTokenLifetimeMatchesApplesCap:
    """Every expiry test derives its clock advance from the constant, so the
    constant itself was never pinned, and Apple rejects anything over 20 min."""

    def test_it_is_twenty_minutes(self):
        from app_reviews.appstore.auth import TOKEN_EXPIRY_SECONDS

        assert TOKEN_EXPIRY_SECONDS == 20 * 60

    def test_the_signed_exp_claim_agrees(self):
        import base64
        import json as _json

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        from app_reviews.appstore.auth import ConnectAuth

        pem = (
            ec.generate_private_key(ec.SECP256R1())
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
        )
        token = ConnectAuth(
            ConnectCredentials(key_id="K", issuer_id="I", private_key=pem)
        ).generate_token(issued_at=1_000_000)
        payload_b64 = token.split(".")[1]
        claims = _json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        )

        assert claims["exp"] - claims["iat"] == 20 * 60

    def test_a_concurrent_close_cannot_produce_a_bearer_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``close()`` lands as the clock is read, before the cache is consulted."""
        auth = ConnectAuth(connect_credentials())
        held = auth.authorization_header()

        def close_then_report_a_time_before_expiry() -> float:
            auth.close()
            return -1.0

        monkeypatch.setattr(
            "app_reviews.appstore.auth.time.time",
            close_then_report_a_time_before_expiry,
        )

        header = auth.authorization_header()

        assert header.startswith("Bearer eyJ")
        assert header != held
