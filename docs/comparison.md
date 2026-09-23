---
description: Audited comparison of app-reviews, focused Python scrapers, Apple RSS, and official review APIs.
---

# Compare App Store and Google Play review clients

Choose by source coverage and operational requirements, not by package name.
`app-reviews` fits Python applications that need one typed model, bounded paging,
and explicit partial-failure data across both stores.

## Audited feature matrix

The cells below reflect public documentation and repository metadata checked on
2026-09-21. “Typed” describes the published Python interface, not the upstream
store payload. “Maintained” reports observable project status rather than
predicting future support.

| Capability | `app-reviews` | `google-play-scraper` | `app-store-scraper` | `app-store-web-scraper` | Apple RSS directly | App Store Connect directly | Google Play Developer API directly |
|---|---|---|---|---|---|---|---|
| Both stores | Yes | No, Play only | No, Apple only | No, Apple only | No, Apple only | No, Apple only | No, Play only |
| Maintained | Current v1 work in 2026 | Not archived; last code push Aug 2024 | Archived/read-only since Oct 2021 | Not archived; last code push Jun 2024 | Apple-operated but undocumented | Vendor-supported API | Vendor-supported API |
| Native async | Yes | No documented async API | No | No | Your HTTP client decides | Your HTTP client decides | Your HTTP client decides |
| Typed interface | Ships `py.typed`; dataclasses and typed errors | Declares the `Typing :: Typed` classifier, but its wheel has no `py.typed` marker; dictionary results | No published typing claim | Annotated `AppReview` API and `Typing :: Typed` classifier, but its wheel has no `py.typed` marker | Raw JSON | Generated/manual client decides | Generated/manual client decides |
| Country behavior | Apple RSS per storefront; other review sources global | `country`/`lang` presentation parameters | One Apple storefront per client | One Apple storefront per entry | URL selects Apple storefront | Global request; review territory may be present | Global; no reviewer country field |
| Resumable cursor exposed | Yes, opaque cursors | Yes, continuation token | No | No public cursor; lazy iterator | Numeric page in URL | `links.next` URL | `nextPageToken` |
| Runtime dependency count | 2 (`httpx`, `cryptography`) | 0 | 1 (`requests`) | 1 (`urllib3`) | 0 beyond chosen HTTP stack | Chosen auth/HTTP stack | Chosen auth/HTTP stack |
| CLI | No | No documented CLI | No | No | Not applicable | Not applicable | Not applicable |

Sources used for this audit:

- [`google-play-scraper` repository](https://github.com/JoMingyu/google-play-scraper)
- archived [`app-store-scraper` repository](https://github.com/cowboy-bebug/app-store-scraper)
- [`app-store-web-scraper` repository](https://github.com/futurice/app-store-web-scraper)
- [Apple App Store Connect customer reviews](https://developer.apple.com/documentation/appstoreconnectapi/list-all-customer-reviews-for-an-app)
- [Google Play reviews API](https://developers.google.com/android-publisher/api-ref/rest/v3/reviews/list)

The Apple RSS endpoint is included because all three public Apple paths in the
table ultimately depend on it. It exposes at most ten pages of roughly fifty
reviews for each storefront and has no formal service contract.

## Source behavior inside app-reviews

| Source | Country behavior | Ordering | History limit | Authentication |
|---|---|---|---|---|
| Apple RSS | Per storefront | Newest first | About 500 recent reviews per storefront | None |
| App Store Connect | Global request; review territory may be present | Newest first | No package-imposed time window | Apple credentials |
| Google Play web | Global review corpus | Newest first | No package-imposed time window | None |
| Google Play Developer API | Global review corpus | Not documented | Reviews created or modified in the last seven days | Google credentials |

For exact fields and caveats, read [Source capabilities](reference/capabilities.md).

## Migrating from google-play-scraper

`google-play-scraper` returns review dictionaries and a continuation token:

```python
from google_play_scraper import reviews

rows, continuation_token = reviews("com.spotify.music", count=100)
```

`app-reviews` returns typed reviews plus fetch diagnostics:

```python
from app_reviews import GooglePlayReviews

with GooglePlayReviews() as client:
    result = client.fetch("com.spotify.music", limit=100, max_pages=5)

rows = result.to_dicts()
diagnostics = result.to_dict()
```

Field names and timestamp semantics differ. Treat this as a migration, not a
drop-in import replacement. In particular, Google Play review country is `None`
because neither supported Play review source reports reviewer location.

## When not to use app-reviews

Do not use this package when you need Play Console CSV history, to post developer
replies, to crawl arbitrary store pages, or to run a standalone service without
writing an application wrapper. Prefer a store's official API when policy,
support guarantees, or account-scoped data matter more than credential-free
access. Prefer a focused single-store library when its existing schema is
already the contract used by your application.
