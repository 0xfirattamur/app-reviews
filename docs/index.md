---
description: Typed Python APIs for Apple App Store and Google Play review ingestion, search, and metadata.
---

# App Reviews

Typed review ingestion and app metadata for the **Apple App Store** and
**Google Play Store** with one Python package.

---

## Why App Reviews?

Each store has a different API, authentication and data format. This package puts
both behind one interface.

- **No credentials to start.** The default sources are public endpoints.
- **Both stores, one API.** `AppStoreReviews` and `GooglePlayReviews` follow the same pattern.
- **Apple storefront fetch.** Fetch public RSS reviews across storefronts in a
  single call; Google Play reviews remain global.
- **Optional authenticated access.** Plug in App Store Connect or Google Play
  Developer API credentials for account-scoped review access.
- **Minimal dependencies.** `cryptography` for JWT signing and `httpx` for transport.
- **Real async.** Every entry point has an async twin (`afetch`, `aiter_reviews`, `aiter_pages`, `asearch`, ...) using `httpx.AsyncClient`, not a thread-pool wrapper. See [Async](guide/async.md).
- **Streams or buffers, your choice.** `fetch()` sorts and filters the whole corpus; `iter_reviews()` yields reviews as they arrive so a 155-storefront walk never has to fit in memory. See [Paging and cursors](guide/paging.md).
- **Pooled connections.** Each client holds one `httpx` connection pool, so a multi-page walk costs one TLS handshake, not one per page.
- **Retries, timeouts and proxies.** Configured per client through `RetryConfig` and `proxy=`.
- **JSON-ready output.** `to_dict()` preserves reviews and diagnostics;
  `to_dicts()` returns review rows for JSONL or CSV.
- **Typed and tested.** Strict mypy, and coverage held at 85% or above.

---

## Quick Example

**Apple App Store:**

```python
from app_reviews import AppStoreReviews, Country

with AppStoreReviews() as client:
    result = client.fetch(
        "324684580",
        countries=[Country.US],
        limit=20,
        max_pages=2,
    )

for review in result:
    print(f"[{review.country}] {review.rating}* {review.title}")
```

**Google Play Store:**

```python
from app_reviews import GooglePlayReviews

# Play has one global review corpus, so there is no country to fan out over.
with GooglePlayReviews() as client:
    result = client.fetch("com.spotify.music", limit=20, max_pages=2)

for review in result:
    print(f"{review.rating}* {review.body[:80]}")
```

Both return a `FetchResult` containing reviews and any per-country errors. `FetchResult` is iterable: loop over it directly to get `Review` objects.

---

## Limitations

- **How far back you can reach differs by source**
    - `appstore_scraper`: ~500 most recent reviews per storefront
    - `googleplay_official`: **the last 7 days only**; full history needs a Play Console CSV export, which this package does not read
    - `appstore_official`, `googleplay_scraper`: unbounded
- **The Google Play web endpoint is undocumented** and rate-limited, and can change without notice
- **Authenticated APIs require developer accounts**
    - Apple Developer Program: $99/year
    - Google Play Developer account: $25 one-time

Per-source detail: [How the sources differ](reference/capabilities.md).

---

## Next Steps

- [Installation](getting-started/installation.md): install the package
- [Quick Start](getting-started/quickstart.md): your first fetch, both stores
- [Python API](guide/python-api.md): full API reference
- [How It Works](reference/how-it-works.md): what the fetch pipeline does
- [Comparison](comparison.md): choose between this package and focused clients
- [FAQ](faq.md): direct answers about countries, limits, and credentials
