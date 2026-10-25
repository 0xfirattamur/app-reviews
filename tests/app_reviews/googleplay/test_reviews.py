"""What ``GooglePlayReviews`` picks, and how it fails when it cannot.

The auth/no-auth choice selects between two sources that behave differently:
``googleplay_official`` needs credentials and guarantees no ordering, while
``googleplay_scraper`` needs none and is newest-first, so which one a given
client resolves to is worth pinning.

Credential problems arrive while building the provider, which is why they are
tested from the client rather than from ``GoogleAuth``: that is the layer a
caller actually holds. Mirrors ``tests/app_reviews/appstore/test_reviews.py``.
"""

import json
import tempfile
from pathlib import Path

import httpx
import pytest

from app_reviews import (
    AppReviewsError,
    AuthError,
    GooglePlayAuth,
    GooglePlayReviews,
    HttpClient,
)
from tests.app_reviews.googleplay.test_auth import _TEST_RSA_KEY


def _client(service_account_path: str) -> GooglePlayReviews:
    return GooglePlayReviews(
        auth=GooglePlayAuth(service_account_path=service_account_path)
    )


def _write(contents: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(contents)
        return f.name


def _service_account(**overrides) -> str:
    """Path to a service-account JSON file, with fields overridden."""
    document = {
        "client_email": "test@test.iam.gserviceaccount.com",
        "private_key": _TEST_RSA_KEY,
        "token_uri": "https://oauth2.googleapis.com/token",
        **overrides,
    }
    return _write(json.dumps(document))


class TestUnusableKeyFile:
    @pytest.mark.parametrize(
        ("label", "path_factory"),
        [
            ("missing", lambda: "/nope/definitely-missing.json"),
            ("a directory", tempfile.mkdtemp),
            ("empty", lambda: _write("")),
            ("not JSON", lambda: _write("not json at all\n")),
            ("JSON but not an object", lambda: _write("[1, 2, 3]")),
            ("no client_email", lambda: _service_account(client_email="")),
            ("no private_key", lambda: _service_account(private_key="")),
            ("private_key is not a PEM", lambda: _service_account(private_key="nope")),
            (
                "token_uri is not HTTPS",
                lambda: _service_account(
                    token_uri="http://oauth2.googleapis.com/token"
                ),
            ),
            (
                "token_uri is not Google",
                lambda: _service_account(token_uri="https://evil.example/token"),
            ),
        ],
    )
    def test_it_raises_auth_error(self, label, path_factory):
        with pytest.raises(AuthError):
            _client(path_factory()).fetch("com.example.app")

    def test_auth_error_is_catchable_as_the_base(self):
        with pytest.raises(AppReviewsError):
            _client("/nope/missing.json").fetch("com.example.app")

    def test_the_message_names_the_path(self):
        with pytest.raises(AuthError, match=r"missing-key\.json"):
            _client("/nope/missing-key.json").fetch("com.example.app")

    def test_the_original_cause_is_preserved(self):
        with pytest.raises(AuthError) as exc:
            _client("/nope/missing.json").fetch("com.example.app")

        assert isinstance(exc.value.__cause__, OSError)

    async def test_the_async_path_raises_the_same(self):
        with pytest.raises(AuthError):
            await _client("/nope/missing.json").afetch("com.example.app")


class TestUsableKeyFile:
    def test_a_real_key_builds_the_official_provider(self):
        assert _client(_service_account()).source == "googleplay_official"

    def test_no_auth_needs_no_key_and_uses_the_scraper(self):
        assert GooglePlayReviews().source == "googleplay_scraper"

    def test_the_key_is_read_once_per_client(self):
        """Provider construction is cached, so the file is not re-read per call."""
        path = Path(_service_account())
        client = _client(str(path))
        assert client.source == "googleplay_official"
        path.unlink()  # gone now; a second read would fail

        assert client.source == "googleplay_official"

    async def test_the_async_path_resolves_the_same_provider(self):
        client = _client(_service_account())

        assert (await client._abuild_provider()).source == "googleplay_official"


class TestTheProviderSharesTheClientsPool:
    def test_the_official_provider_gets_the_clients_http(self):
        """The token exchange and the review requests travel on one pool, so
        proxy and retry cannot diverge between them."""
        client = _client(_service_account())
        provider = client._build_provider()

        assert provider._http is client._http
        assert provider._auth._http is client._http

    def test_the_scraper_provider_gets_the_clients_http(self):
        client = GooglePlayReviews()

        assert client._build_provider()._http is client._http


class TestTranslationLanguage:
    def test_it_reaches_the_developer_api_request(self):
        review_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(
                    200, json={"access_token": "token", "expires_in": 3600}
                )
            review_requests.append(request)
            return httpx.Response(200, json={"reviews": []})

        client = GooglePlayReviews(
            auth=GooglePlayAuth(service_account_path=_service_account()),
            http=HttpClient(transport=httpx.MockTransport(handler)),
            translation_language=" de-DE ",
        )

        client.fetch("com.example.app")

        assert review_requests[0].url.params["translationLanguage"] == "de-DE"

    def test_it_needs_the_developer_api(self):
        with pytest.raises(ValueError, match="needs auth"):
            GooglePlayReviews(translation_language="de")

    @pytest.mark.parametrize("language", ["", "   "])
    def test_a_blank_language_is_rejected(self, language):
        with pytest.raises(ValueError, match="non-empty"):
            GooglePlayReviews(
                auth=GooglePlayAuth(service_account_path=_service_account()),
                translation_language=language,
            )
