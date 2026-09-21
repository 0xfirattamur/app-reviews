---
description: Fetch bounded Apple App Store and Google Play reviews with context-managed Python clients.
---

# Quick Start

Fetching reviews from both stores, with no credentials.

---

## Fetch Apple App Store Reviews

```python
from app_reviews import AppStoreReviews

with AppStoreReviews() as client:
    result = client.fetch("324684580", limit=20, max_pages=2)

for review in result:
    print(f"{review.rating}* {review.title}")
    print(f"  {review.body[:100]}")
    print(f"  by {review.author_name}, {review.country}")
```

Replace `"324684580"` with a numeric App Store ID (the number after `/id` in
the app's URL).

---

## Fetch Google Play Store Reviews

```python
from app_reviews import GooglePlayReviews

with GooglePlayReviews() as client:
    result = client.fetch("com.spotify.music", limit=20, max_pages=2)

for review in result:
    print(f"{review.rating}* {review.body[:100]}")
    print(f"  by {review.author_name}")
```

Replace `"com.spotify.music"` with a package name (the `id` parameter in the
Google Play URL). Google Play review responses do not include reviewer country,
so `review.country` is `None`.

---

## Fetch from Multiple Countries

```python
from app_reviews import AppStoreReviews, Country

with AppStoreReviews() as client:
    result = client.fetch(
        "324684580",
        countries=[Country.US, Country.GB, Country.DE, Country.FR, Country.JP],
        max_pages=5,
    )

print(f"Fetched {len(result)} reviews")
```

---

## Reuse the Client

The client holds connection config (auth, proxy, retry). Reuse it for multiple apps:

```python
from app_reviews import AppStoreReviews, AppStoreAuth, Country

with AppStoreReviews(
    auth=AppStoreAuth(
        key_id="ABC123DEF4",
        issuer_id="12345678-1234-1234-1234-123456789012",
        key_path="/path/to/AuthKey.p8",
    )
) as client:
    # Connect is global: do not pass storefront countries here.
    spotify = client.fetch("324684580", limit=100, max_pages=3)
    instagram = client.fetch("389801252", limit=100, max_pages=3)
```

---

!!! note "`countries=` only applies to the public App Store RSS feed"

    It is the one per-country review source. App Store Connect is global and may
    report a territory on each review. Google Play review clients reject
    `country` and `countries` because neither Play source has a review-country
    dimension. Google Play search and metadata still accept a storefront
    `country` for presentation, availability, and price.

## Filter Results

```python
from datetime import date
from app_reviews import AppStoreReviews, Country

with AppStoreReviews() as client:
    result = client.fetch(
        "324684580",
        countries=[Country.US, Country.GB, Country.DE],
        max_pages=5,
    )

bad_recent = result.filter(ratings=[1, 2], since=date(2025, 1, 1))

for review in bad_recent:
    print(f"{review.rating}* {review.body[:80]}")
```

---

## Understanding the Result

Every `client.fetch()` call returns a `FetchResult`. It is iterable and supports `len()` and `bool()`.

| Property / Method | Description |
|-------------------|-------------|
| `for r in result` | Iterate over `Review` objects. |
| `len(result)` | Number of reviews. |
| `bool(result)` | `True` if there is at least one review. |
| `result.reviews` | The list of `Review` objects. |
| `result.errors` | List of `FetchError` objects for countries that failed. |
| `result.filter(...)` | Returns a new filtered `FetchResult`. |
| `result.sort(...)` | Returns a new sorted `FetchResult`. |
| `result.limit(n)` | Returns a new `FetchResult` truncated to `n` reviews. |
| `result.to_dict()` | Complete JSON-safe envelope with reviews and diagnostics. |
| `result.to_dicts()` | Review rows only, ready for JSONL or guarded CSV export. |

```python
from app_reviews import AppStoreReviews

with AppStoreReviews() as client:
    result = client.fetch("324684580", limit=20, max_pages=2)

print(f"Reviews: {len(result)}")

if result.errors:
    for err in result.errors:
        print(f"Failed: {err.country} ({err.message})")
```

A fetch can partially succeed. If 3 out of 5 countries succeed, you get reviews from those 3 and errors for the other 2.

---

## Next Steps

- [Python API](../guide/python-api.md): all parameters and options
- [Authentication](../guide/authentication.md): set up authenticated APIs
- [Paging and cursors](../guide/paging.md): stream reviews, resume a walk
