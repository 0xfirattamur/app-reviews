"""JWT encoding and ES256 signing utilities."""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils


def encode_base64url(data: bytes) -> str:
    """Base64-URL without padding, which is what RFC 7515 requires."""
    encoded = base64.urlsafe_b64encode(data)
    unpadded = encoded.rstrip(b"=")
    return unpadded.decode("ascii")


def encode_jwt_segment(claims: dict[str, Any]) -> str:
    """One JWT segment: compact JSON, then Base64-URL.

    The separators are given explicitly because ``json.dumps`` otherwise puts a
    space after every ``,`` and ``:``, and a JWT segment has to be the compact
    form.
    """
    compact = json.dumps(claims, separators=(",", ":")).encode()
    return encode_base64url(compact)


def convert_der_to_raw_signature(der_signature: bytes, key_size: int) -> bytes:
    """A DER ECDSA signature as fixed-width raw ``r || s``.

    ``cryptography`` signs to DER, while JWS wants the two integers concatenated
    and each zero-padded to the key's byte width. A DER integer is
    variable-length, so stripping the encoding without re-padding produces a
    signature that verifies only when both halves happen to be full width.
    """
    r, s = utils.decode_dss_signature(der_signature)
    width = (key_size + 7) // 8
    return r.to_bytes(width, "big") + s.to_bytes(width, "big")


def load_ec_private_key_from_pem(pem: str) -> ec.EllipticCurvePrivateKey:
    """An EC private key from PEM, or ``TypeError`` if it is not elliptic-curve.

    Callers have to catch ``TypeError`` as well as the ``ValueError`` an
    unreadable PEM raises: a readable key of the wrong algorithm (an RSA key
    saved as a .p8, easy to do with several keys on one account) parses fine and
    only then fails this type check.

    ``pem`` is unbound before either failure can propagate. It is a parameter, so
    it is live in this frame for as long as the frame is, and both failure paths
    keep the frame alive on a traceback that an error reporter will read the
    locals of. See ``tests/app_reviews/test_credential_hygiene.py``.
    """
    encoded = pem.encode()
    del pem
    try:
        key = serialization.load_pem_private_key(encoded, password=None)
    finally:
        del encoded
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TypeError("Private key must be an EC key.")
    return key
