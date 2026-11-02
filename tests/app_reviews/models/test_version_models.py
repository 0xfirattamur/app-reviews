"""Tests for the version models' JSON conversion."""

import json
from datetime import UTC, datetime

from app_reviews.models.metadata import AppVersionEntry
from app_reviews.models.version import AppStoreVersion

MOMENT = datetime(2024, 3, 15, 10, 0, 0, tzinfo=UTC)


def test_app_version_entry_to_dict_is_json_safe() -> None:
    entry = AppVersionEntry(
        version="9.1.84", released_at=MOMENT, release_notes="Bug fixes"
    )
    assert entry.to_dict() == {
        "version": "9.1.84",
        "released_at": "2024-03-15T10:00:00+00:00",
        "release_notes": "Bug fixes",
    }
    json.dumps(entry.to_dict())


def test_app_store_version_to_dict_is_json_safe() -> None:
    version = AppStoreVersion(
        version_id="v1",
        version="4.0.1",
        platform="IOS",
        state="READY_FOR_DISTRIBUTION",
        release_type="SCHEDULED",
        created_at=MOMENT,
        earliest_release_date=None,
        release_notes={"en-US": "New"},
    )
    assert version.to_dict() == {
        "version_id": "v1",
        "version": "4.0.1",
        "platform": "IOS",
        "state": "READY_FOR_DISTRIBUTION",
        "release_type": "SCHEDULED",
        "created_at": "2024-03-15T10:00:00+00:00",
        "earliest_release_date": None,
        "release_notes": {"en-US": "New"},
    }
    json.dumps(version.to_dict())


def _version(release_notes: dict[str, str]) -> AppStoreVersion:
    return AppStoreVersion(
        version_id="v1",
        version="4.0.1",
        platform="IOS",
        state=None,
        release_type=None,
        created_at=None,
        earliest_release_date=None,
        release_notes=release_notes,
    )


def test_app_store_version_is_hashable_with_release_notes() -> None:
    assert hash(_version({"en-US": "New"})) == hash(_version({"en-US": "New"}))


def test_app_store_version_equality_includes_release_notes() -> None:
    assert _version({"en-US": "New"}) != _version({"en-US": "Other"})
    assert len({_version({"en-US": "New"}), _version({"en-US": "New"})}) == 1
