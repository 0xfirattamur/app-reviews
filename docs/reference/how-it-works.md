---
description: Understand provider selection, paging, filtering, concurrency, and stop reasons in app-reviews.
---

# How It Works

What the package does when you call `client.fetch()`.

---

## The Fetch Pipeline

`fetch()` is the top of a four-rung ladder. Each rung is built on the one
below, so there is exactly one page-walk implementation:

1. **Resolve countries.** Only the App Store RSS feed is per-storefront: the
   requested list for per-country sources, or a single `[""]` call for global
   APIs where the country dimension does not exist. That is the only place the
   fact is recorded; providers do not also answer it. See
   [Source capabilities](capabilities.md).
2. **Walk each country's pages.** One request per page, following the
   provider's opaque cursor.
3. **Stop.** On an exhausted cursor, on `limit`, on a page older than `since`,
   on a cursor the source repeated (`"cycle"`), on a run of review-less pages from
   a source still issuing cursors (`"stalled"`), on the page ceiling
   (`"max_pages"`), or on an error. Which one happened is reported in
   `CountryOutcome.stopped_because`.
4. **Merge, filter, sort, truncate.** Reviews from every country are combined,
   then date and rating filters apply, then the sort, then `limit`.

Countries are fetched concurrently (threads for `fetch()`, `asyncio.gather`
behind a semaphore for `afetch()`), bounded by `concurrency`. See
[Async](../guide/async.md).

To drive the page walk yourself instead of calling `fetch()`, see
[Paging and cursors](../guide/paging.md). To stream reviews without holding them
all in memory, use `iter_reviews()`: step 4 is what forces `fetch()` to buffer
the whole corpus, and `iter_reviews()` is the rung that skips it.

### `since` reduces fetching, it does not just filter

Passing `since` stops the walk once a page's oldest review predates it, so the
remaining requests are never made. This requires the source to return reviews
newest-first. Where that is not
guaranteed, the walk runs to completion and `since` only filters, because a
later page could still hold reviews inside the window.

### `limit` means "the N best under `sort`"

With `sort=Sort.NEWEST` on a newest-first source, `limit` also bounds the walk.
With `Sort.OLDEST` or `Sort.RATING` it cannot: the highest-rated reviews are not
the first ones fetched, so the walk is exhausted before truncating. That is
correct but slower, and it is logged at INFO.

---

## Provider Selection

The provider is selected automatically based on whether you provide auth credentials:

- **With auth**: uses the official API (App Store Connect or Google Play Developer API).
- **Without auth**: uses the free scraper (RSS feed or web scraper).

There is no manual provider override. If you pass credentials, you get the official API.

---

## Providers Overview

| | Apple App Store | Google Play |
|---|---|---|
| **Scraper (free)** | RSS feed. Public, no auth. Max ~500 recent reviews. | Web scraper. Public, no auth. Rate-limited by Google. |
| **Official (auth)** | App Store Connect API. Requires Apple Developer account + API key. | Google Play Developer API. Requires service account. |
| **Source value** | `appstore_scraper` / `appstore_official` | `googleplay_scraper` / `googleplay_official` |

Each source's behavioral differences (ordering guarantees, reply support,
country handling, history depth) are captured as data in
[Source capabilities](capabilities.md).

---

## Data Sources

### Apple App Store: RSS Feed (Scraper)

Public JSON feed. No authentication.

**Endpoint:** `https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/sortBy=mostRecent/page={page}/json`

- Up to 50 reviews per page, paginates through all available pages.
- Returns: review ID, rating, title, body, author, app version, timestamps.
- **Limit:** ~500 most recent reviews per app per country.
- Apple documents no rate limit for this feed.

### Apple App Store: App Store Connect API (Official)

Authenticated REST API for app developers.

**Endpoint:** `https://api.appstoreconnect.apple.com/v1/apps/{app_id}/customerReviews`

- Signs a JWT using your `.p8` private key (ES256).
- You can only access reviews for apps you own.
- Requires Apple Developer Program membership ($99/year).

### Google Play: Web Scraper

Sends requests to Google Play's internal batch endpoint.

**Endpoint:** `https://play.google.com/_/PlayStoreUi/data/batchexecute`

- Up to 200 reviews per request, follows continuation tokens.
- Automatic exponential backoff on rate limits.
- Returns: review ID, rating, body, author, creation timestamp and app version.
- **Undocumented endpoint**: can break if Google changes their internal API.
- Google Play reviews do not have titles.

### Google Play: Developer API (Official)

Authenticated REST API (v3).

**Endpoint:** `https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{app_id}/reviews`

- Signs a JWT using service account key (RS256), exchanges for OAuth2 token.
- Structured pagination.
- Requests up to 100 reviews per page.
- Preserves `reviewerLanguage` and legacy tab-separated titles when present.
- You can only access reviews for apps you own.
- Requires Google Cloud + Google Play Developer account.
- Permissions can take up to 24 hours to propagate.
- **Only the last 7 days are retrievable.** Google documents this limit in a
  `Note:` under "Retrieving a set of reviews" on the [reply-to-reviews
  guide](https://developers.google.com/android-publisher/reply-to-reviews):
  reviews are retrievable only if they were created or modified within the
  last week. Full history requires a CSV export from Play Console, which this
  package does not read. See
  [how the sources differ](capabilities.md#history).
- **No documented ordering.** The API gives no guarantee that pages arrive
  newest-first, so the `since`/`limit` early stop never applies to this source, and
  every fetch walks to exhaustion.

---

## Authentication Flow

### App Store Connect (ES256)

1. Read `.p8` private key.
2. Build JWT with Key ID and Issuer ID.
3. Sign with ES256.
4. Send as `Authorization: Bearer {token}`.
5. Signed once per client, not once per request. Reading the key and signing are
   blocking work, so the async ladder does them in a thread.

### Google Play Developer API (RS256)

1. Read service account JSON, extract RSA private key.
2. Build JWT with `androidpublisher` scope.
3. Sign with RS256.
4. Exchange JWT for OAuth2 access token at `https://oauth2.googleapis.com/token`.
5. Send access token as `Authorization: Bearer {token}`.
6. Exchanged once per client, not once per request, and over the same
   connection pool as the review requests, so it honours the same `proxy` and
   `retry`.

Private keys never leave your machine.

---

## HTTP Layer

All HTTP goes through [`httpx`](https://www.python-httpx.org/), a required
runtime dependency. Every sync call has an async twin using `httpx.AsyncClient`
for real async I/O, not a thread-pool wrapper. See [Async](../guide/async.md).

- **One connection pool per client.** Each client owns an `HttpClient` that
  holds a single `httpx.Client`/`AsyncClient` for its lifetime, so a ten-page
  walk performs one TLS handshake rather than ten. Because the sockets outlive
  the request, close the client when you are done, or use it as a context
  manager:

    ```python
    with AppStoreReviews() as client:
        result = client.fetch("324684580")
    ```

    The async ladder has `aclose()` and `async with`. A client stays usable
    after `close()`; the next request reopens the pool.

- **Retries** with configurable exponential backoff (`RetryConfig`).
- **Timeouts** to prevent hanging requests.
- **Proxy support** via constructor parameter. Pass your own pool with
  `http=HttpClient(...)` to share one between clients or to set a custom
  transport.
- **Classified errors, one vocabulary.** A failed exchange (a bad status, a
  transport failure, or a malformed response body) is classified into an
  `ErrorKind` (`rate_limited`, `auth`, `not_found`, `request`, `server`,
  `transport`, `parse`), so callers branch on `kind` instead of parsing
  exception text. A completed permanent 4xx rejection maps to `request` and
  `RequestError`; it is not retryable. A 401/403 maps to `auth` only when an
  official endpoint received credentials. Credential-free public RSS, web,
  search, and lookup endpoints have no credentials to repair, so their 401/403
  maps to `request` instead. Delivery depends on the layer:
  `fetch`/`iter_pages` walk many pages
  across many countries where partial success is normal, so they report a
  `FetchError` as data; `search`/`lookup` are single requests with a single
  outcome, so they raise: `RateLimitError`, `AuthError`, `RequestError`,
  `ServerError` and the rest, all under `HttpError`/`AppReviewsError`. The class
  is the classification.
  Both are importable from the package root.

---

## Metadata Lookup

The search clients fetch app info without fetching reviews.

```python
from app_reviews import AppStoreSearch, GooglePlaySearch

with AppStoreSearch() as apple, GooglePlaySearch() as play:
    apple_metadata = apple.lookup("123456789")
    play_metadata = play.lookup("com.example.app")
```

- **Apple:** iTunes Lookup API (`https://itunes.apple.com/lookup?id={app_id}`),
  which takes `id` for numeric track ids and `bundleId` otherwise; the client
  picks the right param from the id's shape.
- **Google:** Parses the Google Play store page HTML.
- **Async:** `alookup()` is the async equivalent.
- **No store guessing:** 0.6.0 removed the `lookup_metadata()` helper that
  inferred the store from the id. The caller picks the client, because the caller
  knows the store and the heuristic did not.
