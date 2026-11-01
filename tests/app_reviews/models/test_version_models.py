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
