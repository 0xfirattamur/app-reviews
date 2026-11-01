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
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app_reviews import (
    AppReviewsError,
    AuthError,
    GooglePlayAuth,
    GooglePlayReviews,
)
from tests.app_reviews.factories import service_account_json
from tests.app_reviews.transport import client_for


def _client(service_account_path: str) -> GooglePlayReviews:
    return GooglePlayReviews(
        auth=GooglePlayAuth(service_account_path=service_account_path)
    )


def _service_account(write_file: Callable[[str], str], **overrides) -> str:
    """Path to a service-account JSON file, with fields overridden."""
    return write_file(json.dumps(service_account_json(**overrides)))


class TestUnusableKeyFile:
    @pytest.mark.parametrize(
        ("label", "path_factory"),
        [
            ("missing", lambda write_file: "/nope/definitely-missing.json"),
            ("a directory", lambda write_file: str(Path(write_file("")).parent)),
            ("empty", lambda write_file: write_file("")),
            ("not JSON", lambda write_file: write_file("not json at all\n")),
            ("JSON but not an object", lambda write_file: write_file("[1, 2, 3]")),
            (
                "no client_email",
                lambda write_file: _service_account(write_file, client_email=""),
            ),
            (
                "no private_key",
                lambda write_file: _service_account(write_file, private_key=""),
            ),
            (
                "private_key is not a PEM",
                lambda write_file: _service_account(write_file, private_key="nope"),
            ),
            (
                "token_uri is not HTTPS",
                lambda write_file: _service_account(
                    write_file, token_uri="http://oauth2.googleapis.com/token"
                ),
            ),
            (
                "token_uri is not Google",
                lambda write_file: _service_account(
                    write_file, token_uri="https://evil.example/token"
                ),
            ),
        ],
    )
    def test_it_raises_auth_error(self, label, path_factory, write_file):
        with pytest.raises(AuthError):
            _client(path_factory(write_file)).fetch("com.example.app")

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
    def test_a_real_key_builds_the_official_provider(self, write_file):
        assert _client(_service_account(write_file)).source == "googleplay_official"

    def test_no_auth_needs_no_key_and_uses_the_scraper(self):
        assert GooglePlayReviews().source == "googleplay_scraper"

    def test_the_key_is_read_once_per_client(self, write_file):
        """Provider construction is cached, so the file is not re-read per call."""
        path = Path(_service_account(write_file))
        client = _client(str(path))
        assert client.source == "googleplay_official"
        path.unlink()  # gone now; a second read would fail

        assert client.source == "googleplay_official"

    async def test_the_async_path_resolves_the_same_provider(self, write_file):
        client = _client(_service_account(write_file))

        assert (await client._abuild_provider()).source == "googleplay_official"


class TestTheProviderSharesTheClientsPool:
    def test_the_official_provider_gets_the_clients_http(self, write_file):
        """The token exchange and the review requests travel on one pool, so
        proxy and retry cannot diverge between them."""
        client = _client(_service_account(write_file))
        provider = client._build_provider()

        assert provider._http is client._http
        assert provider._auth._http is client._http

    def test_the_scraper_provider_gets_the_clients_http(self):
        client = GooglePlayReviews()

        assert client._build_provider()._http is client._http


class TestTranslationLanguage:
    def test_it_reaches_the_developer_api_request(self, write_file):
        review_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(
                    200, json={"access_token": "token", "expires_in": 3600}
                )
            review_requests.append(request)
            return httpx.Response(200, json={"reviews": []})

        client = client_for(
            GooglePlayReviews,
            handler,
            auth=GooglePlayAuth(service_account_path=_service_account(write_file)),
            translation_language=" de-DE ",
        )

        client.fetch("com.example.app")

        assert review_requests[0].url.params["translationLanguage"] == "de-DE"

    def test_it_needs_the_developer_api(self):
        with pytest.raises(ValueError, match="needs auth"):
            GooglePlayReviews(translation_language="de")

    @pytest.mark.parametrize("language", ["", "   "])
    def test_a_blank_language_is_rejected(self, language, write_file):
        with pytest.raises(ValueError, match="non-empty"):
            GooglePlayReviews(
                auth=GooglePlayAuth(service_account_path=_service_account(write_file)),
                translation_language=language,
            )
