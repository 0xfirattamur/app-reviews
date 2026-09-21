# app-reviews

Scrape App Store and Google Play reviews in Python with typed sync and async
clients for review ingestion, app search, metadata lookup, and resumable
cursors. Use public sources without credentials, or supply store credentials for
the official review APIs.

[![PyPI](https://img.shields.io/pypi/v/app-reviews.svg)](https://pypi.org/project/app-reviews)
[![Python](https://img.shields.io/pypi/pyversions/app-reviews.svg)](https://pypi.org/project/app-reviews)
[![CI](https://github.com/0xfirattamur/app-reviews/actions/workflows/ci.yml/badge.svg)](https://github.com/0xfirattamur/app-reviews/actions/workflows/ci.yml)
[![E2E](https://github.com/0xfirattamur/app-reviews/actions/workflows/scheduled_e2e_test.yml/badge.svg)](https://github.com/0xfirattamur/app-reviews/actions/workflows/scheduled_e2e_test.yml)
[![License](https://img.shields.io/github/license/0xfirattamur/app-reviews)](LICENSE)

[Documentation](https://0xfirattamur.github.io/app-reviews/) ·
[Comparison](https://0xfirattamur.github.io/app-reviews/comparison/) ·
[FAQ](https://0xfirattamur.github.io/app-reviews/faq/) ·
[Changelog](CHANGELOG.md)

```python
from app_reviews import AppStoreReviews, Country

with AppStoreReviews() as client:
    result = client.fetch(
        "324684580",  # numeric Apple app ID from its App Store URL
        countries=[Country.US, Country.GB],
        limit=20,
        max_pages=2,
    )

for review in result:
    print(review.country, review.rating, review.body[:80])

if result.errors:
    print(result.errors)
```

## Why use it?

- One typed model for Apple App Store and Google Play reviews.
- Sync and native async methods for fetch, paging, search, and lookup.
- Resumable cursors, streaming iterators, request budgets, retry policy, and
  connection pooling.
- Partial failures remain visible through typed errors and per-source outcomes.
- A complete JSON-safe result envelope for automation.
- Search and metadata lookup for both stores.
- Python 3.11+ with `py.typed`; runtime dependencies are `httpx` and
  `cryptography`.

The public endpoints are unofficial and can be throttled or changed by the
stores. Authenticated sources require an Apple Developer or Google Play
Developer account. See [source capabilities](docs/reference/capabilities.md)
before choosing a source.

## Install

```bash
pip install app-reviews
```

Or:

```bash
uv add app-reviews
```

## Fetch reviews

### Apple App Store

The credential-free client uses Apple's public RSS feed. Reviews are partitioned
by storefront, so `countries=` controls which storefronts are fetched.

```python
from app_reviews import AppStoreReviews, Country

with AppStoreReviews() as client:
    result = client.fetch(
        "324684580",
        countries=[Country.US, Country.DE],
        limit=100,
        max_pages=5,
    )
```

Use the numeric Apple app ID—the number after `/id` in an App Store URL. Search
and lookup also accept the identifiers documented in the
[Python API guide](docs/guide/python-api.md).

### Google Play

Google Play reviews form one global corpus. A review response does not include a
reviewer country, so `Review.country` is `None`; a store presentation country is
not a review filter.

```python
from app_reviews import GooglePlayReviews

with GooglePlayReviews() as client:
    result = client.fetch(
        "com.spotify.music",
        limit=100,
        max_pages=5,
    )
```

Use the package name from the `id` query parameter in a Google Play URL.

### Source behavior

| Client configuration | Source | Review country behavior | Credentials |
|---|---|---|---|
| `AppStoreReviews()` | Apple RSS | One corpus per requested storefront | No |
| `AppStoreReviews(auth=...)` | App Store Connect | Global request; territory may be present on each review | Yes |
| `GooglePlayReviews()` | Google Play web | Global; `country=None` | No |
| `GooglePlayReviews(auth=...)` | Google Play Developer API | Global; `country=None` | Yes |

`Review.language` is populated only when a provider reports a language. The
Google Play Developer API can report `reviewerLanguage`; do not interpret that
as reviewer location. Google Play web reviews have no title, while the Developer
API may expose a legacy title embedded in its review text.

## Search and app metadata

Search and lookup are credential-free. Here, `country` selects the storefront
used for availability, presentation, and price; it still does not identify a
reviewer's country.

```python
from app_reviews import AppStoreSearch, Country, GooglePlaySearch

with AppStoreSearch() as apple:
    ios_apps = apple.search("fitness tracker", country=Country.US, limit=5)

with GooglePlaySearch() as play:
    android_apps = play.search("fitness tracker", country=Country.US, limit=5)

print([app.name for app in ios_apps + android_apps])
```

Search returns `list[AppMetadata]`; lookup returns `AppMetadata | None`.

## Results and errors

`fetch()` returns `FetchResult`, which is iterable over `Review` objects. It also
records one `CountryOutcome` for each source walk. A fetch may contain reviews
and errors at the same time.

```python
for outcome in result.outcomes:
    print(
        outcome.country,
        outcome.pages,
        outcome.reviews_fetched,
        outcome.skipped_reviews,
        outcome.stopped_because,
    )

for error in result.errors:
    if error.retryable:
        schedule_retry(error)
```

`stopped_because="exhausted"` means the source reported no next page. Reasons
such as `limit`, `since`, and `max_pages` mean the client stopped while more data
may exist. Malformed records that can be isolated are counted as
`skipped_reviews`; malformed page envelopes are parse errors.

`fetch()` retains partial failures as data. Search and lookup are single-request
operations and raise typed exceptions such as `RateLimitError`, `RequestError`,
`NotFoundError`, and `ParseError`.

## JSON, JSONL, and CSV

Use `result.to_dict()` when diagnostics matter. It returns the full JSON-safe
envelope: `reviews`, `outcomes`, `errors`, and `skipped_reviews`. Use
`result.to_dicts()` only when review rows are all you need. Provider `raw`
payloads are omitted unless `include_raw=True`.

```python
import json

payload = result.to_dict()
print(json.dumps(payload, indent=2))
```

Guard an empty result before deriving CSV headers:

```python
import csv

rows = result.to_dicts()
if rows:
    with open("reviews.csv", "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
```

## Agents and automation

Agents that can call Python functions can use the library directly; an extra
package interface is not required. Keep the function bounded and return the
complete envelope so the caller can distinguish an empty success, a partial
result, and a provider failure.

```python
from typing import Any

from app_reviews import AppStoreReviews, Country


def fetch_app_store_reviews(app_id: str, country: str = "us") -> dict[str, Any]:
    """Fetch at most 50 reviews across at most two provider pages."""
    with AppStoreReviews() as client:
        result = client.fetch(
            app_id,
            countries=[Country(country.lower())],
            limit=50,
            max_pages=2,
        )
    return result.to_dict()
```

Register that function with the agent framework used by your application. Keep
authentication, allowlists, timeouts, retry decisions, and user authorization in
the application layer. The package itself is a Python library and does not run a
network service or accept prompts.

## Async and streaming

Every network entry point has an async twin. Use `iter_reviews()` /
`aiter_reviews()` to avoid buffering a multi-storefront fetch, or `iter_pages()` /
`aiter_pages()` when you need to persist `next_cursor`.

```python
import asyncio

from app_reviews import GooglePlayReviews


async def main() -> None:
    async with GooglePlayReviews() as client:
        result = await client.afetch(
            "com.spotify.music",
            limit=100,
            max_pages=3,
        )
    print(len(result), result.errors)


asyncio.run(main())
```

For Apple RSS, multi-country `fetch()` caps default fan-out at eight workers.
Pass `concurrency=` to choose a smaller or larger explicit limit.

## Official APIs

Pass `AppStoreAuth` to use App Store Connect or `GooglePlayAuth` to use the
Google Play Developer API. The authenticated APIs only expose apps owned by the
credential holder and have different history, ordering, and field behavior.

```python
from app_reviews import AppStoreAuth, AppStoreReviews

auth = AppStoreAuth(
    key_id="ABC123DEF4",
    issuer_id="12345678-1234-1234-1234-123456789012",
    key_path="/path/to/AuthKey.p8",
)

with AppStoreReviews(auth=auth) as client:
    result = client.fetch("324684580", limit=100, max_pages=3)
```

See [authentication](docs/guide/authentication.md) for Google credentials and
setup details.

## Limits worth knowing

- Apple RSS exposes roughly 500 recent reviews per storefront and an empty feed
  cannot distinguish no reviews, an unknown app, and some upstream throttling.
- The Google Play Developer API returns reviews created or modified within the
  last seven days; this package does not read Play Console CSV exports.
- Google Play's public web endpoint is undocumented and can change without
  notice.
- Store terms and applicable law depend on how and where you use the data.
  Review them for your use case.

Read [source capabilities](docs/reference/capabilities.md), the
[comparison](docs/comparison.md), and the [FAQ](docs/faq.md) for details.

## Development

```bash
git clone https://github.com/0xfirattamur/app-reviews.git
cd app-reviews
make install
make all
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Acknowledgements

The Google Play web parsing is based on field-path knowledge from
[google-play-scraper](https://github.com/JoMingyu/google-play-scraper), reworked
on this project's HTTP, retry, paging, and model layers.

## License

[MIT](LICENSE)
