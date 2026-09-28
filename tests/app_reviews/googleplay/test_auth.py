"""Tests for Google Play service account authentication."""

import base64
import json
import urllib.parse
from pathlib import Path
from unittest.mock import mock_open, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization

from app_reviews import GooglePlayReviews
from app_reviews.core.http import HttpClient
from app_reviews.errors import (
    AppReviewsError,
    AuthError,
    ParseError,
    RateLimitError,
    ServerError,
    TransportError,
)
from app_reviews.googleplay.auth import GoogleAuth
from app_reviews.models.config import GooglePlayAuth, RetryConfig

# RSA 2048 test key in PKCS8 format, generated for testing only, not real credentials.
_TEST_RSA_KEY = """\
-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCsfsIYt4li+qCP
ItkaZbm3f6siEa0ItRF98Il4dwdwn1ahSTZNKiY6IVpdkEJSjancWKSrTkRm8RM4
lCSvuxWhYgAfrNtc5cmFbidxBZOlfCtK+uvzNdPKUGALPSorp5BUWALt64T9i2Sb
nNFZy4uDXzcnry0/+Ktacss9JfUzKX8r8sDa20YRvCBftpwJK/nQeNQXT2IbI2IG
f4r2b4UDcrTusA/CGrC8kzV795R0F1rs+ZZf/Q4xZOButuyjm9JUIyP0gPRoOFnN
v8PhbM4l+InKalbSpgJ4poIpnzoCLtTrPly1wzjuDLVbC1vqTkQN66a/ifu3hkET
bMQ9eEw/AgMBAAECggEACmR+tDeGnwCDoOwUEXuUg2I9QMm415/1JFXUKn5dY+Mt
uHztORvzfqgYxi9VV5zyYXSzwyBtjZgztMJ0LWRDqtv5Lu9erguoJxJabPxNh3hi
MVvJ0HbrQjKyfqraOhQX5YUB9Cyu8Amwb5G0qUjSqIEZlRoAS2zjggkNhqsdSU46
5E6ogKXhkpTrAa7KSNOom5llqk8eAkbpbCxWko7Hh/2RGzBrXvPjEe0fIp4JmL9J
n8fvTP8QCndDhyOB3R4DsKckHyvRECZ5bX89zTuCZEHoACRNfl5X4M3ie+WdV0MU
UGiVl7cDRx67NSX/sfgy2G0NKZxFXQQ3ZOFVd2VAgQKBgQDap7KPQSqzdwl/8+hF
geGrKp0hUZjQOZF2SFxamHYxZiSXFWUspqNgGToARAxR92DE+rrQazw8ikFiIXti
3O8i0AmQEgYoJefLyWRdDPDnwBPhZ/udALarE8VobnzFnj7IRGWddZ6jO3WkXp/9
hxyLuCDxGFKV1+BiIrTZbpmbgQKBgQDJ9Mz2WpzyJASzo1mjOGwnuvYqioh78QF9
EHXvP1ETHmrMuVPMSzM8rluinJBQquIQ1gnMSCwFUwc5z2LT8F8GrOyhH4aM2lE6
/3vEa7Dcp8+R2e+LMVmOn6rY+alLplVp4oices+/u/2UzYfPEoOmmEEPRtgHGVh/
E2wzPvzHvwKBgQDH3xFO9+/TmxV4+/kvSFmDwHFf2pnIhUcdi2m4erNX1pnN7YXN
egnPt1+YjJuCdZHknZkVGWE3qd24mQiTx4dlGCwVoRQ1sOihFNgEYub3wVGq8wVC
cDuKSIlkO9McRdn38OW+OJ0pcxnHhaPT+aSzZz3dhTFAvdPjgPw14W7SgQKBgQDF
osqd38ktcbAkOBx1jbbSHrXepdmxKQZi5KP1MQpfYmnz1P27tG/810iX1w2n08MN
5NiL0Kk7wKgWm+xEKjxkZP/hId/joZbS3Mi7TQQ0vAh23Eb++ZmB6KEiWxihDrMR
vv4f3FPId+lOIODh9WVeuCsbDyJDuDsRiOlfMSnhMwKBgDh3v+7dI2E9pIWDzW6R
BGvqX5FnWN4OCbyl98cSo99qXFnLqifFpVfJjhdxk5uDsD5mqwl9GbhNhDFrSVst
OZyyCpMAriwhjl+6B6UT9xwDaqIq+8wxl4zjn4ekjZuT5q19kNC5kWqBKxZs61hG
sFAt2f21YJ6sthPw/SAGgy8t
-----END PRIVATE KEY-----
"""

_SERVICE_ACCOUNT_JSON = {
    "client_email": "test@test.iam.gserviceaccount.com",
    "private_key": _TEST_RSA_KEY,
    "token_uri": "https://oauth2.googleapis.com/token",
}


def _write_service_account(tmp_path: Path) -> Path:
    """Write a minimal service-account JSON key file and return its path."""
    key_file = tmp_path / "service-account.json"
    key_file.write_text(json.dumps(_SERVICE_ACCOUNT_JSON))
    return key_file


class TestGoogleAuthInit:
    def test_loads_service_account_from_file(self):
        data = json.dumps(_SERVICE_ACCOUNT_JSON)
        with patch("builtins.open", mock_open(read_data=data)):
            auth = GoogleAuth("/fake/path.json")
        assert auth._credentials.client_email == "test@test.iam.gserviceaccount.com"

    def test_the_1_1_keyword_still_loads_the_file(self, tmp_path):
        """1.0 and 1.1 documented ``service_account_path=``; 1.2 is a minor release."""
        key_file = str(_write_service_account(tmp_path))

        for auth in (
            GoogleAuth(key_file),
            GoogleAuth(service_account_path=key_file),
        ):
            assert (
                auth._credentials.client_email == _SERVICE_ACCOUNT_JSON["client_email"]
            )

    def test_service_account_info_needs_no_file(self):
        with patch("builtins.open", side_effect=AssertionError("file read")):
            auth = GoogleAuth(service_account_info=_SERVICE_ACCOUNT_JSON)

        assert auth._credentials.client_email == _SERVICE_ACCOUNT_JSON["client_email"]

    @pytest.mark.parametrize(
        "sources",
        [{}, {"service_account_path": "sa.json", "service_account_info": {}}],
    )
    def test_exactly_one_source_is_required(self, sources):
        with pytest.raises(ValueError, match="exactly one"):
            GoogleAuth(**sources)

    def test_a_key_file_missing_client_email_is_an_auth_error(self):
        data = json.dumps({"private_key": _TEST_RSA_KEY})
        with (
            patch("builtins.open", mock_open(read_data=data)),
            pytest.raises(AuthError, match="client_email"),
        ):
            GoogleAuth("/fake/path.json")


class TestGoogleAuthBuildJwt:
    def _make_auth(self):
        data = json.dumps(_SERVICE_ACCOUNT_JSON)
        with patch("builtins.open", mock_open(read_data=data)):
            return GoogleAuth("/fake/path.json")

    def test_jwt_has_three_parts(self):
        auth = self._make_auth()
        jwt = auth._build_jwt()
        parts = jwt.split(".")
        assert len(parts) == 3

    def test_jwt_header_contains_rs256(self):
        import base64

        auth = self._make_auth()
        jwt = auth._build_jwt()
        header_b64 = jwt.split(".")[0]
        # Re-pad base64
        padded = header_b64 + "=" * (4 - len(header_b64) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded))
        assert header["alg"] == "RS256"
        assert header["typ"] == "JWT"

    def test_jwt_payload_contains_expected_claims(self):
        import base64

        auth = self._make_auth()
        jwt = auth._build_jwt()
        payload_b64 = jwt.split(".")[1]
        padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        assert payload["iss"] == "test@test.iam.gserviceaccount.com"
        assert "androidpublisher" in payload["scope"]
        assert payload["exp"] == payload["iat"] + 3600


def _auth_on(handler):
    """A GoogleAuth whose token exchange runs on a MockTransport-backed pool."""
    data = json.dumps(_SERVICE_ACCOUNT_JSON)
    with patch("builtins.open", mock_open(read_data=data)):
        return GoogleAuth(
            "/fake/path.json",
            http=HttpClient(transport=httpx.MockTransport(handler)),
        )


def _token(access_token="ya29.test-token"):
    def handler(_request):
        return httpx.Response(200, json={"access_token": access_token})

    return handler


class TestGoogleAuthAuthorizationHeader:
    def test_returns_bearer_token(self):
        assert _auth_on(_token()).authorization_header() == "Bearer ya29.test-token"

    def test_sends_jwt_assertion_in_body(self):
        seen = {}

        def handler(request):
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "tok"})

        _auth_on(handler).authorization_header()

        assert "assertion=" in seen["body"]
        assert "grant_type=" in seen["body"]

    def test_posts_to_the_token_uri_as_a_form(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["type"] = request.headers.get("content-type")
            return httpx.Response(200, json={"access_token": "tok"})

        _auth_on(handler).authorization_header()

        assert seen["url"] == "https://oauth2.googleapis.com/token"
        assert seen["type"] == "application/x-www-form-urlencoded"

    def test_non_200_raises_auth_error(self):
        def handler(_request):
            return httpx.Response(401, text="unauthorized")

        with pytest.raises(AuthError, match="401"):
            _auth_on(handler).authorization_header()

    def test_missing_access_token_raises_auth_error(self):
        def handler(_request):
            return httpx.Response(200, json={})

        with pytest.raises(ParseError, match="no access_token"):
            _auth_on(handler).authorization_header()

    def test_transport_error_does_not_report_http_0(self):
        def handler(_request):
            raise httpx.ConnectError("Connection refused")

        with pytest.raises(TransportError, match="Connection refused") as exc_info:
            _auth_on(handler).authorization_header()

        assert "HTTP 0" not in str(exc_info.value)

    def test_the_exchange_runs_on_the_pool_it_was_given(self):
        """FIX: a proxied/retrying client's token exchange must not bypass the
        proxy/retry it was configured with. That used to mean forwarding both
        down to ``http_post``; now the caller hands over the whole pool, so
        proxy and retry cannot diverge from the one the reviews travel on.
        """
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"access_token": "tok"})

        _auth_on(handler).authorization_header()

        assert calls == ["https://oauth2.googleapis.com/token"]

    def test_retry_config_on_the_pool_governs_the_exchange(self, monkeypatch):
        monkeypatch.setattr("app_reviews.core.http.time.sleep", lambda _d: None)
        attempts = []

        def handler(_request):
            attempts.append(1)
            return httpx.Response(503, text="")

        data = json.dumps(_SERVICE_ACCOUNT_JSON)
        with patch("builtins.open", mock_open(read_data=data)):
            auth = GoogleAuth(
                "/fake/path.json",
                http=HttpClient(
                    transport=httpx.MockTransport(handler),
                    retry=RetryConfig(max_retries=2, backoff_factor=0),
                ),
            )

        with pytest.raises(ServerError):
            auth.authorization_header()

        assert len(attempts) == 3


class TestTokenExchangeFailuresAreClassified:
    """A dead connection and a revoked credential are not the same problem.

    All three failure modes used to raise a bare ``AuthError``, so a caller either
    retried permanently broken credentials or gave up on a working account over one
    timeout. Only a rejected credential is an ``AuthError`` now.
    """

    def test_revoked_credentials_are_an_auth_error(self):
        def handler(_request):
            return httpx.Response(401, text="invalid_grant")

        with pytest.raises(AuthError) as exc:
            _auth_on(handler).authorization_header()

        assert exc.value.status == 401

    def test_a_dead_connection_is_a_transport_error(self):
        def handler(_request):
            raise httpx.ConnectError("no route to host")

        with pytest.raises(TransportError):
            _auth_on(handler).authorization_header()

    def test_googles_own_failure_is_a_server_error(self):
        def handler(_request):
            return httpx.Response(500, text="oops")

        with pytest.raises(ServerError) as exc:
            _auth_on(handler).authorization_header()

        assert exc.value.status == 500

    def test_a_throttled_exchange_reports_the_asked_wait(self):
        def handler(_request):
            return httpx.Response(429, headers={"Retry-After": "45"})

        with pytest.raises(RateLimitError) as exc:
            _auth_on(handler).authorization_header()

        assert (exc.value.status, exc.value.retry_after) == (429, 45.0)

    def test_a_token_less_success_is_a_parse_error(self):
        def handler(_request):
            return httpx.Response(200, json={"expires_in": 3600})

        with pytest.raises(ParseError):
            _auth_on(handler).authorization_header()

    def test_all_of_them_remain_catchable_as_one(self):
        """A caller that only wants "auth setup failed" still has one net."""
        from app_reviews import AppReviewsError

        def handler(_request):
            raise httpx.ConnectError("boom")

        with pytest.raises(AppReviewsError):
            _auth_on(handler).authorization_header()


class TestAsyncTokenExchange:
    async def test_aauthorization_header_returns_bearer_token(self):
        auth = _auth_on(_token("at-123"))

        assert await auth.aauthorization_header() == "Bearer at-123"

    async def test_aauthorization_header_matches_sync(self):
        assert _auth_on(_token("at-123")).authorization_header() == (
            await _auth_on(_token("at-123")).aauthorization_header()
        )

    async def test_async_failure_raises_auth_error(self):
        def handler(_request):
            return httpx.Response(401, text="denied")

        with pytest.raises(AuthError, match="401"):
            await _auth_on(handler).aauthorization_header()

    async def test_async_transport_error_does_not_report_http_0(self):
        def handler(_request):
            raise httpx.ConnectError("Connection refused")

        with pytest.raises(TransportError, match="Connection refused") as exc_info:
            await _auth_on(handler).aauthorization_header()

        assert "HTTP 0" not in str(exc_info.value)

    async def test_the_async_exchange_also_runs_on_the_given_pool(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"access_token": "tok"})

        await _auth_on(handler).aauthorization_header()

        assert calls == ["https://oauth2.googleapis.com/token"]


class TestTheReviewClientSharesItsPool:
    """The token exchange and the review requests travel on one pool.

    Previously ``GooglePlayReviews`` forwarded ``proxy`` and ``retry`` into a
    fresh ``GoogleAuth``, which forwarded them again into a per-request
    ``httpx.Client``. Handing over the pool itself removes both hops, and with
    them the chance that the exchange is configured differently from the fetch.
    """

    def test_both_the_token_and_the_reviews_go_through_the_injected_pool(
        self, tmp_path
    ):
        key_file = _write_service_account(tmp_path)
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if "oauth2" in str(request.url):
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, text=json.dumps({"reviews": []}))

        client = GooglePlayReviews(
            auth=GooglePlayAuth(service_account_path=str(key_file)),
            http=HttpClient(transport=httpx.MockTransport(handler)),
        )
        result = client.fetch("com.example.app")

        assert result.reviews == []
        assert calls[0] == "https://oauth2.googleapis.com/token"
        assert "/applications/com.example.app/reviews" in calls[1]

    async def test_the_async_ladder_shares_the_pool_too(self, tmp_path):
        key_file = _write_service_account(tmp_path)
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if "oauth2" in str(request.url):
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, text=json.dumps({"reviews": []}))

        client = GooglePlayReviews(
            auth=GooglePlayAuth(service_account_path=str(key_file)),
            http=HttpClient(transport=httpx.MockTransport(handler)),
        )
        await client.afetch("com.example.app")

        assert calls[0] == "https://oauth2.googleapis.com/token"

    def test_in_memory_service_account_info_needs_no_file(self):
        seen = []

        def handler(request):
            seen.append(request)
            if "oauth2" in str(request.url):
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, text=json.dumps({"reviews": []}))

        with patch("builtins.open", side_effect=AssertionError("file read")):
            client = GooglePlayReviews(
                auth=GooglePlayAuth(service_account_info=dict(_SERVICE_ACCOUNT_JSON)),
                http=HttpClient(transport=httpx.MockTransport(handler)),
            )
            client.fetch("com.example.app")

        assertion = urllib.parse.parse_qs(seen[0].content.decode())["assertion"][0]
        claims = json.loads(base64.urlsafe_b64decode(assertion.split(".")[1] + "=="))
        assert claims["iss"] == _SERVICE_ACCOUNT_JSON["client_email"]
        assert seen[1].headers["authorization"] == "Bearer tok"

    @pytest.mark.parametrize(
        "sources",
        [{}, {"service_account_path": "sa.json", "service_account_info": {}}],
    )
    def test_exactly_one_key_source_is_required(self, sources):
        with pytest.raises(ValueError, match="exactly one"):
            GooglePlayAuth(**sources)


def _sa(**overrides) -> str:
    """A service-account document as JSON text, with fields overridden."""
    document = {**_SERVICE_ACCOUNT_JSON, **overrides}
    return json.dumps(document)


def _auth_from(text: str, handler=None) -> GoogleAuth:
    """Build a GoogleAuth from raw key-file text, on a mocked pool."""
    transport = httpx.MockTransport(handler or _token())
    with patch("builtins.open", mock_open(read_data=text)):
        return GoogleAuth(
            "/fake/path.json",
            http=HttpClient(transport=transport),
        )


class TestCredentialFailuresAreAuthErrors:
    """Loading a key file must fail as ``AuthError``, not as a stdlib exception.

    A caller cannot be expected to catch ``FileNotFoundError`` and
    ``json.JSONDecodeError`` alongside this package's own errors. The App Store
    path already reports these as ``AuthError``; this one did not.
    """

    def test_a_missing_file_is_an_auth_error(self, tmp_path):
        with pytest.raises(AuthError, match="Cannot read"):
            GoogleAuth(str(tmp_path / "nope.json"))

    def test_a_directory_instead_of_a_file_is_an_auth_error(self, tmp_path):
        with pytest.raises(AuthError, match="Cannot read"):
            GoogleAuth(str(tmp_path))

    def test_unreadable_json_is_an_auth_error(self):
        with (
            patch("builtins.open", mock_open(read_data="{not json")),
            pytest.raises(AuthError, match="not valid JSON"),
        ):
            GoogleAuth("/fake/path.json")

    def test_a_json_document_that_is_not_an_object_is_an_auth_error(self):
        with (
            patch("builtins.open", mock_open(read_data="[1, 2, 3]")),
            pytest.raises(AuthError, match="not a JSON object"),
        ):
            GoogleAuth("/fake/path.json")

    def test_a_key_that_is_not_pem_is_an_auth_error(self):
        with (
            patch("builtins.open", mock_open(read_data=_sa(private_key="oops"))),
            pytest.raises(AuthError, match="unusable"),
        ):
            GoogleAuth("/fake/path.json")

    def test_an_unparseable_pem_is_an_auth_error_at_signing(self):
        """``ServiceAccountCredentials`` only checks the PEM markers, so a key
        well-formed outside and unreadable inside fails at the first signature."""
        pem = "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n"
        auth = _auth_from(_sa(private_key=pem))

        with pytest.raises(AuthError, match="could not be loaded"):
            auth.authorization_header()

    def test_a_non_rsa_key_is_an_auth_error(self):
        """Google signs these assertions with RSA; an EC key reached
        ``key.sign`` and failed as a stdlib TypeError."""
        from cryptography.hazmat.primitives.asymmetric import ec

        ec_pem = (
            ec.generate_private_key(ec.SECP256R1())
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
        )
        auth = _auth_from(_sa(private_key=ec_pem))

        with pytest.raises(AuthError, match="RSA"):
            auth.authorization_header()


class TestTokenUriIsValidated:
    """``token_uri`` comes out of the key file and is where the signed assertion
    is POSTed, so it is checked rather than trusted."""

    @pytest.mark.parametrize(
        "uri",
        [
            "https://oauth2.googleapis.com/token",
            "https://sts.googleapis.com/v1/token",
        ],
    )
    def test_a_google_token_endpoint_is_accepted(self, uri):
        auth = _auth_from(_sa(token_uri=uri))

        assert auth.authorization_header().startswith("Bearer ")

    def test_plaintext_http_is_rejected(self):
        with (
            patch(
                "builtins.open",
                mock_open(
                    read_data=_sa(token_uri="http://oauth2.googleapis.com/token")
                ),
            ),
            pytest.raises(AuthError, match="non-HTTPS"),
        ):
            GoogleAuth("/fake/path.json")

    @pytest.mark.parametrize(
        "uri",
        [
            "https://evil.example/collect",
            "https://googleapis.com.evil.example/token",
            "https://notgoogleapis.com/token",
        ],
    )
    def test_a_non_google_host_is_rejected(self, uri):
        with (
            patch("builtins.open", mock_open(read_data=_sa(token_uri=uri))),
            pytest.raises(AuthError, match="not a Google token endpoint"),
        ):
            GoogleAuth("/fake/path.json")


class TestTheKeyIsParsedOnce:
    def test_the_pem_is_parsed_once_across_refreshes(self):
        """Signing happens per assertion; parsing the RSA key does not need to."""
        auth = _auth_from(_sa())
        parses = {"n": 0}
        real = serialization.load_pem_private_key

        def counting(*args, **kwargs):
            parses["n"] += 1
            return real(*args, **kwargs)

        with patch(
            "app_reviews.googleplay.auth.serialization.load_pem_private_key", counting
        ):
            for _ in range(3):
                auth._expires_at = 0.0
                auth.authorization_header()

        assert parses["n"] == 1


class TestExpiresIn:
    """The exchange succeeded and a token is in hand, so an unusable
    ``expires_in`` only decides when to re-exchange; it must not raise."""

    def _header_with(self, payload: dict) -> GoogleAuth:
        def handler(_request):
            return httpx.Response(200, json=payload)

        return _auth_from(_sa(), handler)

    @pytest.mark.parametrize("value", ["soon", None, [], 0, -5])
    def test_an_unusable_expires_in_falls_back_to_the_default(self, value):
        auth = self._header_with({"access_token": "T", "expires_in": value})

        assert auth.authorization_header() == "Bearer T"
        assert auth._expires_at > 0

    def test_a_numeric_string_is_accepted(self):
        auth = self._header_with({"access_token": "T", "expires_in": "120"})
        auth.authorization_header()

        # 120s minus the refresh margin, so well under the 3600 default.
        assert auth._expires_at < __import__("time").time() + 200


class TestAsyncSigningStaysOffTheLoop:
    """Building and signing the assertion is CPU work with nothing to await."""

    async def test_the_assertion_is_not_signed_on_the_event_loop_thread(self):
        import threading

        auth = _auth_from(_sa())
        loop_thread = threading.get_ident()
        signed_on: dict[str, int] = {}

        def record():
            signed_on["thread"] = threading.get_ident()
            return "header.payload.signature"

        with patch.object(auth, "_build_jwt", record):
            await auth.aauthorization_header()

        assert signed_on["thread"] != loop_thread


class TestTheReviewClientBuildsOffTheLoop:
    async def test_the_service_account_file_is_not_read_on_the_loop_thread(
        self, tmp_path
    ):
        """``_build_provider`` reads the key JSON off disk, which blocks and has
        no async equivalent to await."""
        import threading

        key_file = _write_service_account(tmp_path)
        client = GooglePlayReviews(
            auth=GooglePlayAuth(service_account_path=str(key_file))
        )
        loop_thread = threading.get_ident()
        built_on: dict[str, int] = {}
        build = client._build_provider

        def record():
            built_on["thread"] = threading.get_ident()
            return build()

        with patch.object(client, "_build_provider", record):
            await client._abuild_provider()

        assert built_on["thread"] != loop_thread


class TestTheTokenExchangeDoesNotFollowRedirects:
    """The signed assertion travels in the POST body, which a 307/308 re-sends
    verbatim to the next host, and header stripping does not cover it."""

    def test_a_redirect_off_the_token_host_is_not_followed(self):
        hops = []

        def handler(request):
            hops.append(str(request.url))
            if "oauth2.googleapis.com" in str(request.url):
                return httpx.Response(
                    307, headers={"Location": "https://evil.example/collect"}
                )
            return httpx.Response(200, json={"access_token": "leaked"})

        with pytest.raises(AppReviewsError):
            _auth_on(handler).authorization_header()

        assert hops == ["https://oauth2.googleapis.com/token"]

    async def test_the_async_exchange_agrees(self):
        hops = []

        def handler(request):
            hops.append(str(request.url))
            if "oauth2.googleapis.com" in str(request.url):
                return httpx.Response(
                    308, headers={"Location": "https://evil.example/collect"}
                )
            return httpx.Response(200, json={"access_token": "leaked"})

        with pytest.raises(AppReviewsError):
            await _auth_on(handler).aauthorization_header()

        assert hops == ["https://oauth2.googleapis.com/token"]


class TestTheTokenHostAllowlistIsExact:
    """``googleapis.com`` is a shared multi-tenant namespace, so a suffix match
    accepted every Google-hosted endpoint as a destination for the assertion."""

    @pytest.mark.parametrize(
        "uri",
        [
            "https://storage.googleapis.com/attacker-bucket/collect",
            "https://anything-at-all.googleapis.com/token",
            "https://googleapis.com/token",
        ],
    )
    def test_a_non_token_google_host_is_rejected(self, uri):
        with (
            patch("builtins.open", mock_open(read_data=_sa(token_uri=uri))),
            pytest.raises(AuthError, match="not a Google token endpoint"),
        ):
            GoogleAuth("/fake/path.json")

    @pytest.mark.parametrize(
        "uri",
        ["https://oauth2.googleapis.com/token", "https://sts.googleapis.com/v1/token"],
    )
    def test_the_real_token_endpoints_are_accepted(self, uri):
        assert (
            _auth_from(_sa(token_uri=uri)).authorization_header().startswith("Bearer")
        )
