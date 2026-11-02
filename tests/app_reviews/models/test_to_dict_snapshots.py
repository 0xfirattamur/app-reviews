"""Characterization of every ``to_dict`` output, one fully populated model each."""

import json
from datetime import UTC, datetime

from app_reviews.models.metadata import AppMetadata
from app_reviews.models.page import PageResult
from app_reviews.models.reply import ReviewReply
from app_reviews.models.result import CountryOutcome, FetchError, FetchResult
from app_reviews.models.review import DeviceMetadata
from tests.app_reviews.factories import make_review

MOMENT = datetime(2024, 3, 15, 10, 0, 0, tzinfo=UTC)


def snapshot(value: dict) -> str:
    return json.dumps(value, sort_keys=True)


def key_order_snapshot(value: dict) -> str:
    """Pin key order too: consumers write these dicts out without sorting."""
    return json.dumps(value)


def full_error(country: str = "us") -> FetchError:
    return FetchError(country=country, message="boom", kind="rate_limited", status=429)


def full_review():
    return make_review(
        language="en",
        app_version="1.2",
        raw={"nested": [1, {"a": None}]},
        developer_reply=ReviewReply(
            review_id="scraper-1",
            reply_id="reply-1",
            text="Thanks",
            state="published",
            updated_at=MOMENT,
        ),
        original_text="Original",
        device="walleye",
        device_metadata=DeviceMetadata(
            product_name="Pixel",
            manufacturer="Google",
            device_class="phone",
            screen_width_px=1080,
            screen_height_px=1920,
            native_platform="arm64-v8a",
            screen_density_dpi=420,
            gl_es_version=196610,
            cpu_model="Snapdragon",
            cpu_make="Qualcomm",
            ram_mb=4096,
        ),
        thumbs_up_count=3,
        thumbs_down_count=1,
        app_version_code=42,
        android_os_version=34,
    )


def full_outcome() -> CountryOutcome:
    return CountryOutcome(
        country="us",
        pages=2,
        reviews_fetched=7,
        stopped_because="exhausted",
        elapsed=1.5,
        error=full_error(),
        skipped_reviews=1,
        feed_format="xml",
        xml_fallback_error=full_error("gb"),
    )


def test_review_to_dict() -> None:
    review = full_review()
    assert snapshot(review.to_dict()) == snapshot(REVIEW)
    assert key_order_snapshot(review.to_dict()) == key_order_snapshot(REVIEW)
    assert snapshot(review.to_dict(include_raw=True)) == snapshot(REVIEW_WITH_RAW)
    assert key_order_snapshot(review.to_dict(include_raw=True)) == key_order_snapshot(
        REVIEW_WITH_RAW
    )


def test_review_raw_is_passed_by_reference() -> None:
    review = full_review()
    assert review.to_dict(include_raw=True)["raw"] is review.raw


def test_device_metadata_to_dict() -> None:
    device_metadata = full_review().device_metadata
    assert snapshot(device_metadata.to_dict()) == snapshot(DEVICE_METADATA)
    assert key_order_snapshot(device_metadata.to_dict()) == key_order_snapshot(
        DEVICE_METADATA
    )


def test_review_reply_to_dict() -> None:
    assert snapshot(full_review().developer_reply.to_dict()) == snapshot(REVIEW_REPLY)
    assert key_order_snapshot(
        full_review().developer_reply.to_dict()
    ) == key_order_snapshot(REVIEW_REPLY)


def test_app_metadata_to_dict() -> None:
    metadata = AppMetadata(
        app_id="1",
        store="appstore",
        name="App",
        developer="Dev",
        category="Tools",
        price="Free",
        version="1.0",
        rating=4.5,
        rating_count=10,
        url="https://example.com",
        icon_url="https://example.com/icon.png",
        current_version_release_date=MOMENT,
        first_release_date=MOMENT,
    )
    assert snapshot(metadata.to_dict()) == snapshot(APP_METADATA)
    assert key_order_snapshot(metadata.to_dict()) == key_order_snapshot(APP_METADATA)


def test_fetch_error_to_dict() -> None:
    assert snapshot(full_error().to_dict()) == snapshot(FETCH_ERROR)
    assert key_order_snapshot(full_error().to_dict()) == key_order_snapshot(FETCH_ERROR)


def test_country_outcome_to_dict() -> None:
    assert snapshot(full_outcome().to_dict()) == snapshot(COUNTRY_OUTCOME)
    assert key_order_snapshot(full_outcome().to_dict()) == key_order_snapshot(
        COUNTRY_OUTCOME
    )


def test_page_result_to_dict() -> None:
    page = PageResult(
        reviews=[full_review()],
        next_cursor="cursor",
        error=full_error(),
        stopped_because="limit",
        skipped_reviews=2,
        feed_format="json",
        xml_fallback_error=full_error("gb"),
    )
    assert snapshot(page.to_dict()) == snapshot(PAGE_RESULT)
    assert key_order_snapshot(page.to_dict()) == key_order_snapshot(PAGE_RESULT)
    assert snapshot(page.to_dict(include_raw=True)) == snapshot(PAGE_RESULT_WITH_RAW)
    assert key_order_snapshot(page.to_dict(include_raw=True)) == key_order_snapshot(
        PAGE_RESULT_WITH_RAW
    )


def test_fetch_result_to_dict() -> None:
    result = FetchResult(reviews=[full_review()], outcomes=[full_outcome()])
    assert snapshot(result.to_dict()) == snapshot(FETCH_RESULT)
    assert key_order_snapshot(result.to_dict()) == key_order_snapshot(FETCH_RESULT)
    assert snapshot(result.to_dict(include_raw=True)) == snapshot(FETCH_RESULT_WITH_RAW)
    assert key_order_snapshot(result.to_dict(include_raw=True)) == key_order_snapshot(
        FETCH_RESULT_WITH_RAW
    )


REVIEW = {
    "store": "appstore",
    "app_id": "12345",
    "country": "us",
    "rating": 5,
    "title": "Great app",
    "body": "Love it",
    "author_name": "Alice",
    "source": "appstore_scraper",
    "id": "scraper-1",
    "created_at": "2024-03-15T10:00:00+00:00",
    "updated_at": None,
    "app_version": "1.2",
    "language": "en",
    "fetched_at": "2024-03-15T10:00:00+00:00",
    "developer_reply": {
        "review_id": "scraper-1",
        "reply_id": "reply-1",
        "text": "Thanks",
        "state": "published",
        "updated_at": "2024-03-15T10:00:00+00:00",
    },
    "original_text": "Original",
    "device": "walleye",
    "device_metadata": {
        "product_name": "Pixel",
        "manufacturer": "Google",
        "device_class": "phone",
        "screen_width_px": 1080,
        "screen_height_px": 1920,
        "native_platform": "arm64-v8a",
        "screen_density_dpi": 420,
        "gl_es_version": 196610,
        "cpu_model": "Snapdragon",
        "cpu_make": "Qualcomm",
        "ram_mb": 4096,
    },
    "thumbs_up_count": 3,
    "thumbs_down_count": 1,
    "app_version_code": 42,
    "android_os_version": 34,
}


REVIEW_WITH_RAW = {
    "store": "appstore",
    "app_id": "12345",
    "country": "us",
    "rating": 5,
    "title": "Great app",
    "body": "Love it",
    "author_name": "Alice",
    "source": "appstore_scraper",
    "id": "scraper-1",
    "created_at": "2024-03-15T10:00:00+00:00",
    "updated_at": None,
    "app_version": "1.2",
    "language": "en",
    "fetched_at": "2024-03-15T10:00:00+00:00",
    "raw": {"nested": [1, {"a": None}]},
    "developer_reply": {
        "review_id": "scraper-1",
        "reply_id": "reply-1",
        "text": "Thanks",
        "state": "published",
        "updated_at": "2024-03-15T10:00:00+00:00",
    },
    "original_text": "Original",
    "device": "walleye",
    "device_metadata": {
        "product_name": "Pixel",
        "manufacturer": "Google",
        "device_class": "phone",
        "screen_width_px": 1080,
        "screen_height_px": 1920,
        "native_platform": "arm64-v8a",
        "screen_density_dpi": 420,
        "gl_es_version": 196610,
        "cpu_model": "Snapdragon",
        "cpu_make": "Qualcomm",
        "ram_mb": 4096,
    },
    "thumbs_up_count": 3,
    "thumbs_down_count": 1,
    "app_version_code": 42,
    "android_os_version": 34,
}


DEVICE_METADATA = {
    "product_name": "Pixel",
    "manufacturer": "Google",
    "device_class": "phone",
    "screen_width_px": 1080,
    "screen_height_px": 1920,
    "native_platform": "arm64-v8a",
    "screen_density_dpi": 420,
    "gl_es_version": 196610,
    "cpu_model": "Snapdragon",
    "cpu_make": "Qualcomm",
    "ram_mb": 4096,
}


REVIEW_REPLY = {
    "review_id": "scraper-1",
    "reply_id": "reply-1",
    "text": "Thanks",
    "state": "published",
    "updated_at": "2024-03-15T10:00:00+00:00",
}


APP_METADATA = {
    "app_id": "1",
    "store": "appstore",
    "name": "App",
    "developer": "Dev",
    "category": "Tools",
    "price": "Free",
    "version": "1.0",
    "rating": 4.5,
    "rating_count": 10,
    "url": "https://example.com",
    "icon_url": "https://example.com/icon.png",
    "current_version_release_date": "2024-03-15T10:00:00+00:00",
    "first_release_date": "2024-03-15T10:00:00+00:00",
    "release_notes": None,
}


FETCH_ERROR = {
    "country": "us",
    "message": "boom",
    "kind": "rate_limited",
    "status": 429,
    "retryable": True,
}


COUNTRY_OUTCOME = {
    "country": "us",
    "pages": 2,
    "reviews_fetched": 7,
    "skipped_reviews": 1,
    "stopped_because": "exhausted",
    "elapsed": 1.5,
    "error": {
        "country": "us",
        "message": "boom",
        "kind": "rate_limited",
        "status": 429,
        "retryable": True,
    },
    "feed_format": "xml",
    "xml_fallback_error": {
        "country": "gb",
        "message": "boom",
        "kind": "rate_limited",
        "status": 429,
        "retryable": True,
    },
}


PAGE_RESULT = {
    "reviews": [
        {
            "store": "appstore",
            "app_id": "12345",
            "country": "us",
            "rating": 5,
            "title": "Great app",
            "body": "Love it",
            "author_name": "Alice",
            "source": "appstore_scraper",
            "id": "scraper-1",
            "created_at": "2024-03-15T10:00:00+00:00",
            "updated_at": None,
            "app_version": "1.2",
            "language": "en",
            "fetched_at": "2024-03-15T10:00:00+00:00",
            "developer_reply": {
                "review_id": "scraper-1",
                "reply_id": "reply-1",
                "text": "Thanks",
                "state": "published",
                "updated_at": "2024-03-15T10:00:00+00:00",
            },
            "original_text": "Original",
            "device": "walleye",
            "device_metadata": {
                "product_name": "Pixel",
                "manufacturer": "Google",
                "device_class": "phone",
                "screen_width_px": 1080,
                "screen_height_px": 1920,
                "native_platform": "arm64-v8a",
                "screen_density_dpi": 420,
                "gl_es_version": 196610,
                "cpu_model": "Snapdragon",
                "cpu_make": "Qualcomm",
                "ram_mb": 4096,
            },
            "thumbs_up_count": 3,
            "thumbs_down_count": 1,
            "app_version_code": 42,
            "android_os_version": 34,
        }
    ],
    "skipped_reviews": 2,
    "next_cursor": "cursor",
    "error": {
        "country": "us",
        "message": "boom",
        "kind": "rate_limited",
        "status": 429,
        "retryable": True,
    },
    "stopped_because": "limit",
    "feed_format": "json",
    "xml_fallback_error": {
        "country": "gb",
        "message": "boom",
        "kind": "rate_limited",
        "status": 429,
        "retryable": True,
    },
}


PAGE_RESULT_WITH_RAW = {
    "reviews": [
        {
            "store": "appstore",
            "app_id": "12345",
            "country": "us",
            "rating": 5,
            "title": "Great app",
            "body": "Love it",
            "author_name": "Alice",
            "source": "appstore_scraper",
            "id": "scraper-1",
            "created_at": "2024-03-15T10:00:00+00:00",
            "updated_at": None,
            "app_version": "1.2",
            "language": "en",
            "fetched_at": "2024-03-15T10:00:00+00:00",
            "raw": {"nested": [1, {"a": None}]},
            "developer_reply": {
                "review_id": "scraper-1",
                "reply_id": "reply-1",
                "text": "Thanks",
                "state": "published",
                "updated_at": "2024-03-15T10:00:00+00:00",
            },
            "original_text": "Original",
            "device": "walleye",
            "device_metadata": {
                "product_name": "Pixel",
                "manufacturer": "Google",
                "device_class": "phone",
                "screen_width_px": 1080,
                "screen_height_px": 1920,
                "native_platform": "arm64-v8a",
                "screen_density_dpi": 420,
                "gl_es_version": 196610,
                "cpu_model": "Snapdragon",
                "cpu_make": "Qualcomm",
                "ram_mb": 4096,
            },
            "thumbs_up_count": 3,
            "thumbs_down_count": 1,
            "app_version_code": 42,
            "android_os_version": 34,
        }
    ],
    "skipped_reviews": 2,
    "next_cursor": "cursor",
    "error": {
        "country": "us",
        "message": "boom",
        "kind": "rate_limited",
        "status": 429,
        "retryable": True,
    },
    "stopped_because": "limit",
    "feed_format": "json",
    "xml_fallback_error": {
        "country": "gb",
        "message": "boom",
        "kind": "rate_limited",
        "status": 429,
        "retryable": True,
    },
}


FETCH_RESULT = {
    "schema_version": 1,
    "reviews": [
        {
            "store": "appstore",
            "app_id": "12345",
            "country": "us",
            "rating": 5,
            "title": "Great app",
            "body": "Love it",
            "author_name": "Alice",
            "source": "appstore_scraper",
            "id": "scraper-1",
            "created_at": "2024-03-15T10:00:00+00:00",
            "updated_at": None,
            "app_version": "1.2",
            "language": "en",
            "fetched_at": "2024-03-15T10:00:00+00:00",
            "developer_reply": {
                "review_id": "scraper-1",
                "reply_id": "reply-1",
                "text": "Thanks",
                "state": "published",
                "updated_at": "2024-03-15T10:00:00+00:00",
            },
            "original_text": "Original",
            "device": "walleye",
            "device_metadata": {
                "product_name": "Pixel",
                "manufacturer": "Google",
                "device_class": "phone",
                "screen_width_px": 1080,
                "screen_height_px": 1920,
                "native_platform": "arm64-v8a",
                "screen_density_dpi": 420,
                "gl_es_version": 196610,
                "cpu_model": "Snapdragon",
                "cpu_make": "Qualcomm",
                "ram_mb": 4096,
            },
            "thumbs_up_count": 3,
            "thumbs_down_count": 1,
            "app_version_code": 42,
            "android_os_version": 34,
        }
    ],
    "outcomes": [
        {
            "country": "us",
            "pages": 2,
            "reviews_fetched": 7,
            "skipped_reviews": 1,
            "stopped_because": "exhausted",
            "elapsed": 1.5,
            "error": {
                "country": "us",
                "message": "boom",
                "kind": "rate_limited",
                "status": 429,
                "retryable": True,
            },
            "feed_format": "xml",
            "xml_fallback_error": {
                "country": "gb",
                "message": "boom",
                "kind": "rate_limited",
                "status": 429,
                "retryable": True,
            },
        }
    ],
    "errors": [
        {
            "country": "us",
            "message": "boom",
            "kind": "rate_limited",
            "status": 429,
            "retryable": True,
        }
    ],
    "xml_fallback_errors": [
        {
            "country": "gb",
            "message": "boom",
            "kind": "rate_limited",
            "status": 429,
            "retryable": True,
        }
    ],
    "skipped_reviews": 1,
}


FETCH_RESULT_WITH_RAW = {
    "schema_version": 1,
    "reviews": [
        {
            "store": "appstore",
            "app_id": "12345",
            "country": "us",
            "rating": 5,
            "title": "Great app",
            "body": "Love it",
            "author_name": "Alice",
            "source": "appstore_scraper",
            "id": "scraper-1",
            "created_at": "2024-03-15T10:00:00+00:00",
            "updated_at": None,
            "app_version": "1.2",
            "language": "en",
            "fetched_at": "2024-03-15T10:00:00+00:00",
            "raw": {"nested": [1, {"a": None}]},
            "developer_reply": {
                "review_id": "scraper-1",
                "reply_id": "reply-1",
                "text": "Thanks",
                "state": "published",
                "updated_at": "2024-03-15T10:00:00+00:00",
            },
            "original_text": "Original",
            "device": "walleye",
            "device_metadata": {
                "product_name": "Pixel",
                "manufacturer": "Google",
                "device_class": "phone",
                "screen_width_px": 1080,
                "screen_height_px": 1920,
                "native_platform": "arm64-v8a",
                "screen_density_dpi": 420,
                "gl_es_version": 196610,
                "cpu_model": "Snapdragon",
                "cpu_make": "Qualcomm",
                "ram_mb": 4096,
            },
            "thumbs_up_count": 3,
            "thumbs_down_count": 1,
            "app_version_code": 42,
            "android_os_version": 34,
        }
    ],
    "outcomes": [
        {
            "country": "us",
            "pages": 2,
            "reviews_fetched": 7,
            "skipped_reviews": 1,
            "stopped_because": "exhausted",
            "elapsed": 1.5,
            "error": {
                "country": "us",
                "message": "boom",
                "kind": "rate_limited",
                "status": 429,
                "retryable": True,
            },
            "feed_format": "xml",
            "xml_fallback_error": {
                "country": "gb",
                "message": "boom",
                "kind": "rate_limited",
                "status": 429,
                "retryable": True,
            },
        }
    ],
    "errors": [
        {
            "country": "us",
            "message": "boom",
            "kind": "rate_limited",
            "status": 429,
            "retryable": True,
        }
    ],
    "xml_fallback_errors": [
        {
            "country": "gb",
            "message": "boom",
            "kind": "rate_limited",
            "status": 429,
            "retryable": True,
        }
    ],
    "skipped_reviews": 1,
}


def test_fetch_result_envelope_starts_with_its_schema_version() -> None:
    assert next(iter(FetchResult().to_dict())) == "schema_version"
    assert FetchResult().to_dict()["schema_version"] == 1
