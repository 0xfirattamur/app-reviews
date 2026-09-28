"""Key material must not survive in a traceback.

``ConnectCredentials.private_key`` and ``ServiceAccountCredentials.private_key_pem``
are ``field(repr=False)`` so the PEM cannot reach a log line, but ``repr`` is only
half of it. An error reporter that captures frame locals (Sentry does, by default,
and it walks ``__cause__``) reads the *locals* of every frame in the traceback, not
the repr of the exception. A loader that binds the raw key to a local and then
raises hands the key to that reporter.

These tests assert the invariant directly rather than any one implementation of
it: no frame reachable from the raised error holds the secret.
"""

from __future__ import annotations

import json

import pytest

from app_reviews.appstore.auth import ConnectAuth, load_connect_credentials
from app_reviews.core.jwt import load_ec_private_key_from_pem
from app_reviews.errors import AuthError
from app_reviews.googleplay.auth import GoogleAuth
from app_reviews.models.config import AppStoreAuth, GooglePlayAuth

SECRET = "SUPER-SECRET-KEY-MATERIAL-DO-NOT-LEAK"
_PEM_SHAPED = f"-----BEGIN PRIVATE KEY-----\n{SECRET}\n"
_BAD_SERVICE_ACCOUNT_INFO = {
    "empty_client_email": {"client_email": "", "private_key": SECRET},
    "foreign_token_uri": {
        "client_email": "a@b.iam.gserviceaccount.com",
        "private_key": _PEM_SHAPED,
        "token_uri": "https://evil.test/token",
    },
}


def _frames_holding(exc: BaseException, needle: str) -> list[str]:
    """Every frame reachable from ``exc`` whose locals contain ``needle``.

    Follows ``__cause__`` and ``__context__``, because both keep the frames of
    the original failure alive and a reporter walks them.
    """
    seen: set[int] = set()
    holding: list[str] = []
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        tb = current.__traceback__
        while tb is not None:
            if needle in repr(tb.tb_frame.f_locals):
                holding.append(
                    f"{type(current).__name__} -> {tb.tb_frame.f_code.co_name}"
                )
            tb = tb.tb_next
        stack += [current.__cause__, current.__context__]
    return holding


class TestTheAppStoreKeyDoesNotReachATraceback:
    def test_an_unusable_p8_fails_without_carrying_the_key(self, tmp_path):
        """A readable file that is not a usable key: the caller sees ``AuthError``,
        the reporter sees no PEM."""
        key_path = tmp_path / "key.p8"
        key_path.write_text(SECRET, encoding="utf-8")

        with pytest.raises(AuthError) as caught:
            load_connect_credentials(
                AppStoreAuth(key_id="k", issuer_id="i", key_path=str(key_path))
            )

        assert _frames_holding(caught.value, SECRET) == []

    def test_an_empty_key_id_does_not_carry_the_key_either(self, tmp_path):
        """The key is valid-shaped here; a *different* field is what fails, and the
        key must still not travel."""
        key_path = tmp_path / "key.p8"
        key_path.write_text(
            f"-----BEGIN PRIVATE KEY-----\n{SECRET}\n", encoding="utf-8"
        )

        with pytest.raises(AuthError) as caught:
            load_connect_credentials(
                AppStoreAuth(key_id="", issuer_id="i", key_path=str(key_path))
            )

        assert _frames_holding(caught.value, SECRET) == []

    @pytest.mark.parametrize("case", ["not_pem", "empty_key_id"])
    def test_an_unusable_in_memory_key_fails_without_carrying_it(self, case):
        """Looked up by name, so the test's own frame never binds the key."""
        with pytest.raises(AuthError) as caught:
            load_connect_credentials(
                AppStoreAuth(
                    key_id="" if case == "empty_key_id" else "k",
                    issuer_id="i",
                    private_key=_PEM_SHAPED if case == "empty_key_id" else SECRET,
                )
            )

        assert SECRET not in str(caught.value)
        assert _frames_holding(caught.value, SECRET) == []

    def test_an_in_memory_pem_that_does_not_parse_fails_at_signing_without_it(self):
        credentials = load_connect_credentials(
            AppStoreAuth(key_id="k", issuer_id="i", private_key=_PEM_SHAPED)
        )

        with pytest.raises(AuthError) as caught:
            ConnectAuth(credentials).authorization_header()

        assert SECRET not in str(caught.value)
        assert _frames_holding(caught.value, SECRET) == []

    def test_passing_both_sources_fails_without_carrying_the_key(self):
        with pytest.raises(ValueError) as caught:
            AppStoreAuth(key_id="k", issuer_id="i", key_path="k.p8", private_key=SECRET)

        assert SECRET not in str(caught.value)
        assert _frames_holding(caught.value, SECRET) == []

    def test_repr_never_shows_the_in_memory_key(self):
        auth = AppStoreAuth(key_id="k", issuer_id="i", private_key=SECRET)

        assert SECRET not in repr(auth)


class TestTheGoogleKeyDoesNotReachATraceback:
    def test_an_unusable_service_account_fails_without_carrying_it(self, tmp_path):
        """``_load`` binds the whole parsed document, so a leak here is the entire
        service-account file, not just the key."""
        path = tmp_path / "sa.json"
        path.write_text(
            json.dumps({"client_email": "", "private_key": SECRET}), encoding="utf-8"
        )

        with pytest.raises(AuthError) as caught:
            GoogleAuth(GooglePlayAuth(service_account_path=str(path)))

        assert _frames_holding(caught.value, SECRET) == []

    def test_a_non_google_token_uri_does_not_carry_the_key(self, tmp_path):
        """The rejected value is the ``token_uri``; the key is incidental and must
        not ride along."""
        path = tmp_path / "sa.json"
        path.write_text(
            json.dumps(
                {
                    "client_email": "a@b.iam.gserviceaccount.com",
                    "private_key": f"-----BEGIN PRIVATE KEY-----\n{SECRET}\n",
                    "token_uri": "https://evil.test/token",
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(AuthError) as caught:
            GoogleAuth(GooglePlayAuth(service_account_path=str(path)))

        assert _frames_holding(caught.value, SECRET) == []

    @pytest.mark.parametrize("case", sorted(_BAD_SERVICE_ACCOUNT_INFO))
    def test_unusable_service_account_info_fails_without_carrying_it(self, case):
        """Looked up by name, so the test's own frame never binds the key."""
        with pytest.raises(AuthError) as caught:
            GoogleAuth(
                GooglePlayAuth(service_account_info=_BAD_SERVICE_ACCOUNT_INFO[case])
            )

        assert SECRET not in str(caught.value)
        assert _frames_holding(caught.value, SECRET) == []

    def test_passing_both_sources_fails_without_carrying_the_key(self):
        with pytest.raises(ValueError) as caught:
            GooglePlayAuth(
                service_account_path="sa.json",
                service_account_info={"private_key": SECRET},
            )

        assert SECRET not in str(caught.value)
        assert _frames_holding(caught.value, SECRET) == []

    def test_repr_never_shows_the_service_account_info(self):
        auth = GooglePlayAuth(service_account_info={"private_key": SECRET})

        assert SECRET not in repr(auth)


class TestTheJwtLoaderDoesNotReachATraceback:
    def test_an_unreadable_pem_does_not_carry_it(self):
        """``load_pem_private_key`` raises ``ValueError`` from a frame below, but
        this function's own frame still binds ``pem``."""
        with pytest.raises(ValueError) as caught:
            load_ec_private_key_from_pem(SECRET)

        assert _frames_holding(caught.value, SECRET) == []

    def test_a_wrong_algorithm_key_does_not_carry_it(self):
        """The documented trap: an RSA key saved as a .p8 parses fine and fails the
        type check, so ``pem`` is live in the frame that raises."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_pem = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            .decode()
        )

        with pytest.raises(TypeError) as caught:
            load_ec_private_key_from_pem(rsa_pem)

        assert _frames_holding(caught.value, rsa_pem) == []
