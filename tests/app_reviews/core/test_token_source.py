"""Auth tokens expire, so a provider must ask for one per request.

Providers used to take ``auth_header: str``, minted when the provider was built
and cached for the client's life. Measured against the real Connect API, that
string carried ``exp = now + 1200s``, so a client held open past twenty minutes
401'd on every request. Google Play had the same shape with a one-hour fuse.

The provider now holds a ``TokenSource`` and asks it per request; the source
caches and re-mints on expiry, which is the only place the expiry is known.
"""

import json

import httpx
import pytest

from app_reviews.appstore.auth import TOKEN_EXPIRY_SECONDS, ConnectAuth
from app_reviews.appstore.connect import AppStoreOfficialProvider
from app_reviews.core.http import HttpClient
from app_reviews.googleplay.auth import GoogleAuth
from app_reviews.googleplay.developer_api import GooglePlayOfficialProvider
from app_reviews.models.config import ConnectCredentials, GooglePlayAuth
from tests.app_reviews.appstore.test_auth import _TEST_PRIVATE_KEY


def _creds() -> ConnectCredentials:
    return ConnectCredentials(
        key_id="K7YXJ5RHF2", issuer_id="issuer-uuid", private_key=_TEST_PRIVATE_KEY
    )


class _Clock:
    """A movable stand-in for ``time.time``."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestConnectAuthCaches:
    def test_a_fresh_token_is_reused(self, monkeypatch):
        monkeypatch.setattr("app_reviews.appstore.auth.time.time", _Clock())
        auth = ConnectAuth(_creds())

        assert auth.authorization_header() == auth.authorization_header()

    def test_the_pem_is_parsed_once_not_per_token(self, monkeypatch):
        """Two tokens, one PEM parse: the expensive part is the key, not the sign."""
        clock = _Clock()
        monkeypatch.setattr("app_reviews.appstore.auth.time.time", clock)
        parses = []
        import app_reviews.appstore.auth as mod

        real = mod.load_ec_private_key_from_pem

        def spy(pem):
            parses.append(1)
            return real(pem)

        monkeypatch.setattr(mod, "load_ec_private_key_from_pem", spy)
        auth = ConnectAuth(_creds())
        first = auth.authorization_header()
        clock.advance(TOKEN_EXPIRY_SECONDS)
        second = auth.authorization_header()

        assert first != second  # a second token really was minted
        assert len(parses) == 1

    def test_an_expiring_token_is_re_minted(self, monkeypatch):
        clock = _Clock()
        monkeypatch.setattr("app_reviews.appstore.auth.time.time", clock)
        auth = ConnectAuth(_creds())
        first = auth.authorization_header()

        clock.advance(TOKEN_EXPIRY_SECONDS)

        assert auth.authorization_header() != first

    def test_it_refreshes_before_expiry_not_after(self, monkeypatch):
        """A request starting a second before exp would arrive already stale."""
        clock = _Clock()
        monkeypatch.setattr("app_reviews.appstore.auth.time.time", clock)
        auth = ConnectAuth(_creds())
        first = auth.authorization_header()

        clock.advance(TOKEN_EXPIRY_SECONDS - 5)

        assert auth.authorization_header() != first

    async def test_the_async_twin_returns_the_same_token(self, monkeypatch):
        monkeypatch.setattr("app_reviews.appstore.auth.time.time", _Clock())
        auth = ConnectAuth(_creds())

        assert await auth.aauthorization_header() == auth.authorization_header()


def _token_pool(handler):
    return HttpClient(transport=httpx.MockTransport(handler))


class TestGoogleAuthCaches:
    def _auth(self, handler, **kw):
        data = json.dumps(
            {
                "client_email": "x@y.iam.gserviceaccount.com",
                "private_key": _GOOGLE_KEY,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
        from unittest.mock import mock_open, patch

        with patch("builtins.open", mock_open(read_data=data)):
            return GoogleAuth(
                GooglePlayAuth(service_account_path="/fake.json"),
                http=_token_pool(handler),
                **kw,
            )

    def test_one_exchange_serves_repeated_calls(self, monkeypatch):
        monkeypatch.setattr("app_reviews.googleplay.auth.time.time", _Clock())
        exchanges = []

        def handler(_r):
            exchanges.append(1)
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})

        auth = self._auth(handler)
        auth.authorization_header()
        auth.authorization_header()

        assert len(exchanges) == 1

    def test_expires_in_drives_the_refresh(self, monkeypatch):
        clock = _Clock()
        monkeypatch.setattr("app_reviews.googleplay.auth.time.time", clock)
        exchanges = []

        def handler(_r):
            exchanges.append(1)
            return httpx.Response(
                200, json={"access_token": f"t{len(exchanges)}", "expires_in": 100}
            )

        auth = self._auth(handler)
        first = auth.authorization_header()
        clock.advance(100)
        second = auth.authorization_header()

        assert len(exchanges) == 2
        assert first != second

    async def test_the_async_path_shares_the_cache(self, monkeypatch):
        monkeypatch.setattr("app_reviews.googleplay.auth.time.time", _Clock())
        exchanges = []

        def handler(_r):
            exchanges.append(1)
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})

        auth = self._auth(handler)
        auth.authorization_header()
        await auth.aauthorization_header()

        assert len(exchanges) == 1


class _RecordingSource:
    """A TokenSource that reports how often it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    def authorization_header(self) -> str:
        self.calls += 1
        return f"Bearer token-{self.calls}"

    async def aauthorization_header(self) -> str:
        return self.authorization_header()


@pytest.mark.parametrize(
    ("provider_cls", "app_id", "body"),
    [
        (AppStoreOfficialProvider, "123", '{"data": []}'),
        (GooglePlayOfficialProvider, "com.x", '{"reviews": []}'),
    ],
)
class TestProvidersAskPerRequest:
    def test_each_request_gets_a_current_header(self, provider_cls, app_id, body):
        source = _RecordingSource()
        seen = []

        def handler(request):
            seen.append(request.headers["authorization"])
            return httpx.Response(200, text=body)

        provider = provider_cls(source, http=_token_pool(handler))
        provider.fetch_page(app_id, "", None)
        provider.fetch_page(app_id, "", None)

        assert source.calls == 2
        assert seen == ["Bearer token-1", "Bearer token-2"]

    async def test_the_async_path_asks_too(self, provider_cls, app_id, body):
        source = _RecordingSource()

        def handler(_request):
            return httpx.Response(200, text=body)

        provider = provider_cls(source, http=_token_pool(handler))
        await provider.afetch_page(app_id, "", None)
        await provider.afetch_page(app_id, "", None)

        assert source.calls == 2


_GOOGLE_KEY = """\
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


class TestTheClockIsReadOnce:
    """The JWT's ``exp`` and the local expiry must come from one clock read, or
    what the object believes about its token can drift from what it signed.
    """

    def test_the_local_expiry_matches_the_signed_exp(self, monkeypatch):
        import base64
        import json

        clock = _Clock()
        monkeypatch.setattr("app_reviews.appstore.auth.time.time", clock)
        auth = ConnectAuth(_creds())
        header = auth.authorization_header()

        payload = header.removeprefix("Bearer ").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        signed_exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]

        from app_reviews.appstore.auth import REFRESH_MARGIN_SECONDS

        assert auth._expires_at == signed_exp - REFRESH_MARGIN_SECONDS

    def test_it_stops_using_the_token_before_apple_would_reject_it(self, monkeypatch):
        from app_reviews.appstore.auth import REFRESH_MARGIN_SECONDS

        clock = _Clock()
        monkeypatch.setattr("app_reviews.appstore.auth.time.time", clock)
        auth = ConnectAuth(_creds())
        first = auth.authorization_header()

        clock.advance(TOKEN_EXPIRY_SECONDS - REFRESH_MARGIN_SECONDS - 1)
        assert auth.authorization_header() == first  # still inside the margin

        clock.advance(2)
        assert auth.authorization_header() != first  # margin crossed
