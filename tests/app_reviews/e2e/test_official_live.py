"""Live tests for the official-API reply and version clients.

Read-only unless explicitly opted in. Each test skips, naming what is missing,
when its credentials are not in the environment:

- App Store: ``APP_REVIEWS_E2E_ASC_KEY_ID``, ``APP_REVIEWS_E2E_ASC_ISSUER_ID``,
  ``APP_REVIEWS_E2E_ASC_PRIVATE_KEY_PATH``, ``APP_REVIEWS_E2E_ASC_APP_ID`` and
  ``APP_REVIEWS_E2E_ASC_REVIEW_ID``.
- Google Play: ``APP_REVIEWS_E2E_PLAY_SERVICE_ACCOUNT_PATH``,
  ``APP_REVIEWS_E2E_PLAY_PACKAGE`` and ``APP_REVIEWS_E2E_PLAY_REVIEW_ID``.

The write tests publish a real, public reply. They also need
``APP_REVIEWS_E2E_WRITE=1`` plus a review chosen for it
(``APP_REVIEWS_E2E_ASC_WRITE_REVIEW_ID`` / ``APP_REVIEWS_E2E_PLAY_WRITE_REVIEW_ID``),
and are never part of a routine run.

Run with: uv run pytest -m live tests/app_reviews/e2e/test_official_live.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app_reviews import (
    AppStoreAuth,
    AppStoreReplies,
    AppStoreVersions,
    GooglePlayAuth,
    GooglePlayReplies,
    ReviewReply,
)

_ASC = (
    "APP_REVIEWS_E2E_ASC_KEY_ID",
    "APP_REVIEWS_E2E_ASC_ISSUER_ID",
    "APP_REVIEWS_E2E_ASC_PRIVATE_KEY_PATH",
    "APP_REVIEWS_E2E_ASC_APP_ID",
    "APP_REVIEWS_E2E_ASC_REVIEW_ID",
)
_PLAY = (
    "APP_REVIEWS_E2E_PLAY_SERVICE_ACCOUNT_PATH",
    "APP_REVIEWS_E2E_PLAY_PACKAGE",
    "APP_REVIEWS_E2E_PLAY_REVIEW_ID",
)
_REPLY_TEXT = "Thank you for the feedback."


def _require(*names: str) -> dict[str, str]:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.skip(f"set {', '.join(missing)} to run this live test")
    return {name: os.environ[name] for name in names}


def _require_write(review_id_var: str) -> str:
    if os.environ.get("APP_REVIEWS_E2E_WRITE") != "1":
        pytest.skip("publishes a real reply; set APP_REVIEWS_E2E_WRITE=1 to opt in")
    return _require(review_id_var)[review_id_var]


@pytest.fixture
def asc() -> dict[str, str]:
    return _require(*_ASC)


@pytest.fixture
def asc_auth(asc: dict[str, str]) -> AppStoreAuth:
    return AppStoreAuth(
        key_id=asc["APP_REVIEWS_E2E_ASC_KEY_ID"],
        issuer_id=asc["APP_REVIEWS_E2E_ASC_ISSUER_ID"],
        key_path=asc["APP_REVIEWS_E2E_ASC_PRIVATE_KEY_PATH"],
    )


@pytest.fixture
def play() -> dict[str, str]:
    return _require(*_PLAY)


@pytest.fixture
def play_auth(play: dict[str, str]) -> GooglePlayAuth:
    return GooglePlayAuth(
        service_account_path=play["APP_REVIEWS_E2E_PLAY_SERVICE_ACCOUNT_PATH"]
    )


@pytest.mark.live
class TestAppStoreOfficialLive:
    def test_versions_lists_the_apps_versions(self, asc, asc_auth):
        with AppStoreVersions(asc_auth) as client:
            versions = client.versions(asc["APP_REVIEWS_E2E_ASC_APP_ID"])

        assert versions
        assert all(v.version and v.platform for v in versions)
        created = [v.created_at for v in versions if v.created_at is not None]
        assert all(c.utcoffset() is not None for c in created)
        assert created == sorted(created, reverse=True)

    async def test_an_in_memory_key_authenticates(self, asc):
        pem = Path(asc["APP_REVIEWS_E2E_ASC_PRIVATE_KEY_PATH"]).read_text(
            encoding="utf-8"
        )
        auth = AppStoreAuth(
            key_id=asc["APP_REVIEWS_E2E_ASC_KEY_ID"],
            issuer_id=asc["APP_REVIEWS_E2E_ASC_ISSUER_ID"],
            private_key=pem,
        )
        del pem

        async with AppStoreVersions(auth) as client:
            versions = await client.aversions(asc["APP_REVIEWS_E2E_ASC_APP_ID"])

        assert versions

    def test_get_reply_reads_a_real_review(self, asc, asc_auth):
        review_id = asc["APP_REVIEWS_E2E_ASC_REVIEW_ID"]

        with AppStoreReplies(asc_auth) as client:
            reply = client.get_reply(review_id)

        assert reply is None or (
            isinstance(reply, ReviewReply)
            and reply.review_id == review_id
            and reply.reply_id
            and reply.state in ("published", "pending")
        )

    def test_reply_publishes_and_reads_back(self, asc, asc_auth):
        review_id = _require_write("APP_REVIEWS_E2E_ASC_WRITE_REVIEW_ID")

        with AppStoreReplies(asc_auth) as client:
            written = client.reply(review_id, _REPLY_TEXT)
            read = client.get_reply(review_id)

        assert written.text == _REPLY_TEXT
        assert read is not None and read.reply_id == written.reply_id


@pytest.mark.live
class TestGooglePlayOfficialLive:
    def test_get_reply_reads_a_real_review(self, play, play_auth):
        review_id = play["APP_REVIEWS_E2E_PLAY_REVIEW_ID"]

        with GooglePlayReplies(play_auth) as client:
            reply = client.get_reply(
                review_id, package_name=play["APP_REVIEWS_E2E_PLAY_PACKAGE"]
            )

        assert reply is None or (
            reply.review_id == review_id and reply.state == "published"
        )

    def test_reply_publishes_and_reads_back(self, play, play_auth):
        review_id = _require_write("APP_REVIEWS_E2E_PLAY_WRITE_REVIEW_ID")
        package = play["APP_REVIEWS_E2E_PLAY_PACKAGE"]

        with GooglePlayReplies(play_auth) as client:
            written = client.reply(review_id, _REPLY_TEXT, package_name=package)
            read = client.get_reply(review_id, package_name=package)

        assert written.text == _REPLY_TEXT
        assert read is not None and read.text == _REPLY_TEXT
