"""Tests for JWT encoding and signing utilities."""

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app_reviews.core.jwt import (
    convert_der_to_raw_signature,
    encode_base64url,
    encode_jwt_segment,
    load_ec_private_key_from_pem,
)
from tests.app_reviews.factories import TEST_EC_PRIVATE_KEY, TEST_RSA_PRIVATE_KEY


class TestEncodeBase64url:
    def test_empty_bytes(self):
        assert encode_base64url(b"") == ""

    def test_strips_padding(self):
        result = encode_base64url(b"a")
        assert "=" not in result

    def test_url_safe_characters(self):
        # Bytes that produce + and / in standard base64
        data = b"\xfb\xff\xfe"
        result = encode_base64url(data)
        assert "+" not in result
        assert "/" not in result

    def test_round_trip(self):
        data = b"hello world"
        encoded = encode_base64url(data)
        padded = encoded + "=" * (4 - len(encoded) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        assert decoded == data


class TestEncodeJwtSegment:
    def test_produces_valid_base64url_json(self):
        claims = {"alg": "ES256", "typ": "JWT"}
        result = encode_jwt_segment(claims)
        padded = result + "=" * (4 - len(result) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        assert decoded == claims

    def test_compact_json_no_spaces(self):
        claims = {"key": "value", "num": 42}
        result = encode_jwt_segment(claims)
        padded = result + "=" * (4 - len(result) % 4)
        raw_json = base64.urlsafe_b64decode(padded).decode()
        assert " " not in raw_json


class TestConvertDerToRawSignature:
    def test_p256_signature_is_64_bytes(self):
        key = ec.generate_private_key(ec.SECP256R1())
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec as ec_mod

        der_sig = key.sign(b"test", ec_mod.ECDSA(hashes.SHA256()))
        raw = convert_der_to_raw_signature(der_sig, 256)
        assert len(raw) == 64

    def test_p384_signature_is_96_bytes(self):
        key = ec.generate_private_key(ec.SECP384R1())
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec as ec_mod

        der_sig = key.sign(b"test", ec_mod.ECDSA(hashes.SHA384()))
        raw = convert_der_to_raw_signature(der_sig, 384)
        assert len(raw) == 96


class TestLoadEcPrivateKeyFromPem:
    def test_loads_valid_ec_key(self):
        key = load_ec_private_key_from_pem(TEST_EC_PRIVATE_KEY)
        assert isinstance(key, ec.EllipticCurvePrivateKey)

    def test_rejects_rsa_key(self):
        with pytest.raises(TypeError, match="EC key"):
            load_ec_private_key_from_pem(TEST_RSA_PRIVATE_KEY)


class TestASignatureActuallyVerifies:
    """``len(raw) == 64`` is the one property that cannot detect the bug this
    conversion exists to prevent. Swapping the ``r`` and ``s`` halves produces a
    correctly-sized signature that Apple rejects on every request."""

    def _key(self):
        from cryptography.hazmat.primitives.asymmetric import ec

        return ec.generate_private_key(ec.SECP256R1())

    def test_the_raw_signature_verifies_against_the_public_key(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        key = self._key()
        message = b"header.payload"
        raw = convert_der_to_raw_signature(
            key.sign(message, ec.ECDSA(hashes.SHA256())), key.key_size
        )

        half = len(raw) // 2
        r = int.from_bytes(raw[:half], "big")
        s = int.from_bytes(raw[half:], "big")
        key.public_key().verify(
            utils.encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256())
        )

    def test_the_halves_are_not_swapped(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        key = self._key()
        message = b"header.payload"
        der = key.sign(message, ec.ECDSA(hashes.SHA256()))
        expected_r, expected_s = utils.decode_dss_signature(der)

        raw = convert_der_to_raw_signature(der, key.key_size)
        half = len(raw) // 2

        assert int.from_bytes(raw[:half], "big") == expected_r
        assert int.from_bytes(raw[half:], "big") == expected_s
