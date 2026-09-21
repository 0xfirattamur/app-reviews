---
description: Reference for Review, FetchResult, outcomes, errors, metadata, countries, and configuration models.
---

# Models

All models are frozen dataclasses with `__slots__`.

!!! note "Frozen means fields cannot be reassigned, not that contents cannot change"

    `frozen=True` rejects `result.reviews = [...]`, but the list itself is a plain
    `list`: `result.reviews.append(...)` and `review.raw["k"] = v` both work and
    mutate the model in place. Nothing in this package does that; `filter`, `sort`
    and `limit` all return new objects. Treat the containers as read-only, and copy
    before mutating if you need to.

---

## Review

A single app review, normalized across all stores and providers.

```python
from app_reviews import Review
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `store` | `"appstore"` or `"googleplay"` | Required | Which store. |
| `app_id` | `str` | Required | App ID or package name. |
| `country` | `str \| None` | Required | Storefront queried, not the reviewer's location. `None` if the source does not report one (`googleplay_official`, `googleplay_scraper`: Play has one global review corpus, so there is no storefront to report). |
| `rating` | `int` | Required | Star rating 1-5. Validated on creation. |
| `title` | `str \| None` | Required | Review title. Google Play web has none; the official API may expose a legacy title. |
| `body` | `str` | Required | Review body text. |
| `author_name` | `str` | Required | Author display name. |
| `source` | `Source` | Required | Data source: `appstore_scraper`, `appstore_official`, `googleplay_scraper`, or `googleplay_official`. |
| `created_at` | `datetime` or `None` | `None` | When the review was posted. `None` where the source reports no creation date. |
| `updated_at` | `datetime` or `None` | `None` | Last edit time. `None` where the source reports no modification date. |
| `app_version` | `str` or `None` | `None` | App version reviewed. |
| `language` | `str` or `None` | `None` | Review language. |
| `id` | `str` | Required | Non-empty raw identifier assigned by the source. See below. |
| `fetched_at` | `datetime` or `None` | `None` | When the review was fetched. |
| `raw` | `dict`, `list` or `None` | `None` | Raw API payload, exactly as the source sent it. Apple and official Play send objects; Play web sends positional arrays. |

Rows are in field order, which is also the positional-constructor order,
though `Review` is far easier to get right with keywords.

### Review IDs

`id` is the identifier the source assigned, passed through unchanged: the App Store RSS `id` or Connect `customerReviews.id`, the Google Play `batchexecute` review id or `androidpublisher` `reviewId`.

!!! warning "IDs are not comparable across sources"

    An `id` is unique within a `(store, source)` pair, but not across sources. The two
    providers for a given store read genuinely different identifier spaces, so the same
    real-world review fetched via `googleplay_scraper` and via `googleplay_official`
    carries two different ids.

    Key deduplication on `(store, source, id)`, and use `source` to tell provenance apart.

For App Store Connect, `customerReviewResponses` requires a Connect `customerReviews.id`. RSS ids are numeric (`14357217033`) and Connect ids are opaque, and Apple exposes no mapping between them: the `customerReviews` endpoint has no id filter and no legacy-id attribute. So an `appstore_scraper` id cannot be used to reply.

Google Play appears to use one identifier space for both providers, so a `googleplay_scraper` id may be usable with `androidpublisher` `reviews.reply`. This package neither implements replies nor tests that, so treat it as unverified.

---

## FetchResult

The return value of `client.fetch()` / `client.afetch()`. Contains reviews, any per-country errors, and a per-country breakdown. Iterable: loop directly to get `Review` objects.

```python
from app_reviews import FetchResult
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `reviews` | `list[Review]` | The fetched reviews, merged across countries, filtered and sorted. |
| `errors` | `list[FetchError]` | Per-country fetch failures. Derived from `outcomes`, so it can never disagree with them or be lost by a transform. |
| `outcomes` | `list[CountryOutcome]` | One entry per country actually walked. See [CountryOutcome](#countryoutcome). |
| `skipped_reviews` | `int` | Total malformed or unusable review rows skipped across all outcomes. Derived from `outcomes`. |

### Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `__iter__()` | `Iterator[Review]` | Iterate over reviews. |
| `__len__()` | `int` | Number of reviews. |
| `__bool__()` | `bool` | `True` if there is at least one review. |
| `filter(ratings, since, until)` | `FetchResult` | Return a new filtered `FetchResult`. |
| `sort(order)` | `FetchResult` | Return a new sorted `FetchResult`. |
| `limit(n)` | `FetchResult` | Return a new `FetchResult` truncated to `n` reviews. |
| `to_dicts(include_raw=False)` | `list[dict]` | JSON-serialisable plain dicts: ISO 8601 timestamps, `raw` omitted unless asked. |
| `to_dict(include_raw=False)` | `dict` | Complete JSON-safe envelope containing `reviews`, `outcomes`, `errors`, and `skipped_reviews`. |

Use `FetchResult.to_dict(include_raw=False)` for automation and persistence when
the difference between an empty success and a failed fetch matters. Use
`to_dicts()` only when review rows are sufficient. Passing `include_raw=True`
includes provider payloads inside serialized reviews.

A fetch can partially succeed. Check `result.errors` to see which countries failed, and `result.outcomes` for the full per-country picture, including countries that succeeded but stopped early on `since` or `limit`.

!!! warning "Errors are more visible than they used to be"
    Older versions discarded a country's page error once that country had
    already yielded some reviews, so a non-empty `FetchResult` could still
    hide a failure. Errors now always reach `result.errors` and the matching
    `CountryOutcome.error`.

---

## FetchError

A per-country fetch failure.

```python
from app_reviews import FetchError
```

| Field | Type | Description |
|-------|------|-------------|
| `country` | `str \| None` | Storefront that failed, or `None` for a global source. |
| `message` | `str` | Error description. |
| `kind` | `ErrorKind` | What kind of failure this was. Branch retry policy on this, not `message`. See [ErrorKind](#errorkind). |
| `status` | `int \| None` | HTTP status code, if the exchange produced one. |
| `retryable` | `bool` | Read-only, derived from `kind`. Prefer deciding policy per `kind` yourself over trusting this. |

---

## What each source actually fills

Measured against the live APIs, not inferred from the schema. A blank cell means
the source never reports that field, so it is always `None`, not "sometimes
missing".

| field | `appstore_scraper` | `appstore_official` | `googleplay_scraper` | `googleplay_official` |
|---|---|---|---|---|
| `id` | yes | yes | yes | yes |
| `store` / `app_id` / `source` | yes | yes | yes | yes |
| `rating` / `body` / `author_name` | yes | yes | yes | yes |
| `fetched_at` | yes | yes | yes | yes |
| `created_at` | - | yes | yes | - |
| `updated_at` | yes | - | - | yes |
| `raw` | yes | yes | yes | yes |
| `country` | yes | yes | - | - |
| `title` | yes | yes | - | legacy text only |
| `app_version` | yes | - | mostly | yes |
| `language` | - | - | - | yes |

Two things worth planning around:

- **The paid API reports less than the free one in places.** App Store Connect
  sends only `body`, `createdDate`, `rating`, `reviewerNickname`, `territory` and
  `title`, so `app_version` is always `None` on `appstore_official` while the RSS
  feed does provide it.
- **Only the Google Play Developer API reports language.** Its
  `reviewerLanguage` value is a language code, not the reviewer's country.
- **No source reports both timestamps.** Exactly one of `created_at`/`updated_at`
  is set for every source this package has, and it is the field that source
  orders by; read `review.dated_at` for whichever is present. The model itself
  only accepts exactly one timestamp. `is_edited` was removed because no source
  provides enough information to derive it consistently.

`country` follows one alphabet everywhere: Connect reports ISO alpha-3
(`"USA"`), which is normalised to the alpha-2 form `Country` uses (`"us"`).
Apple's original value stays in `raw["attributes"]["territory"]`.

## CountryOutcome

What one country's fetch actually did. Part of `FetchResult.outcomes`.

```python
from app_reviews import CountryOutcome
```

| Field | Type | Description |
|-------|------|-------------|
| `country` | `str \| None` | The country walked, or `None` for a global source. |
| `pages` | `int` | Number of pages requested. |
| `reviews_fetched` | `int` | Reviews this country's walk pulled off the wire, **before** the cross-country filter/sort/limit. Compare with `len(result.reviews)`, which is what survived: `fetch(ratings=[5])` makes them differ, and the gap tells you the filter is working. |
| `stopped_because` | `StopReason` | Why the walk ended. See [StopReason](#stopreason). |
| `error` | `FetchError \| None` | Set if the walk ended on an error. |
| `elapsed` | `float` | Wall-clock seconds spent on this country. |
| `skipped_reviews` | `int` | Malformed or unusable review rows skipped during this walk. |

`CountryOutcome.to_dict()` serializes every field and nests the complete
`FetchError` dictionary when an error is present.

`stopped_because` distinguishes "there is no more data" (`"exhausted"`) from
"we stopped asking" (`"limit"`, `"since"`), facts that look identical if you
only see the review count.

---

## PageResult

The result of one provider page request, returned by `fetch_page()` / `afetch_page()`, and yielded by `iter_pages()` / `aiter_pages()`. See [Paging and cursors](../guide/paging.md).

```python
from app_reviews import PageResult
```

| Field | Type | Description |
|-------|------|-------------|
| `reviews` | `list[Review]` | Reviews on this page. |
| `next_cursor` | `str \| None` | Opaque, provider-specific cursor. Persist it verbatim to resume later. `None` means no more pages. |
| `error` | `FetchError \| None` | Set if this page failed. |
| `stopped_because` | `StopReason \| None` | Set only on the final page of an `iter_pages()`/`aiter_pages()` walk. Always `None` on a bare `fetch_page()` call, which has nothing to stop. |
| `skipped_reviews` | `int` | Malformed or unusable review rows skipped while parsing this page. |

`PageResult.to_dict(include_raw=False)` returns a JSON-safe page envelope with
`reviews`, `skipped_reviews`, `next_cursor`, `error`, and `stopped_because`.

---

## ErrorKind

A `Literal` classifying why a fetch failed. Branch retry policy on this rather than on `message` text or a caught exception type.

```python
from app_reviews import ErrorKind
```

| Value | Meaning |
|-------|---------|
| `"rate_limited"` | HTTP 429. |
| `"auth"` | HTTP 401 or 403 from a credentialed official API, or credentials that cannot be used. |
| `"not_found"` | HTTP 404. |
| `"server"` | HTTP 5xx. |
| `"request"` | A completed, permanent HTTP 4xx rejection that is not rate limiting, not-found, or credentialed authentication. This includes 401/403 from credential-free public endpoints. |
| `"transport"` | Connection failure or timeout. |
| `"parse"` | The response body was malformed, not `json.JSONDecodeError` raised out of the call but a classified error you can inspect. |

On single-request operations, the `"request"` kind is raised as
`RequestError`. It is nonretryable. A public RSS, web, search, or lookup endpoint
has no caller credentials to repair, so its 401/403 is also a request rejection;
401/403 is `"auth"`/`AuthError` only for an official credentialed endpoint.

---

## StopReason

A `Literal` reporting why a page walk ended. Appears on `PageResult.stopped_because` (final page only) and `CountryOutcome.stopped_because`.

```python
from app_reviews import StopReason
```

| Value | Meaning |
|-------|---------|
| `"exhausted"` | The provider ran out of pages. There is no more data. |
| `"limit"` | The caller's `limit` was reached. More data may exist. |
| `"since"` | A page predated `since`, so paging stopped early. More data may exist. |
| `"cycle"` | The source repeated a cursor, so following it again would not advance. More data may exist, but this walk cannot reach it. |
| `"stalled"` | The source kept issuing fresh cursors but returned no reviews for several consecutive pages, so it is not advancing. |
| `"max_pages"` | The walk hit its page ceiling. More data may exist; resume from the final page's cursor. |
| `"error"` | The walk failed. See the accompanying `FetchError`. |

`"exhausted"` outranks both `"limit"` and `"since"` when they apply together, so
"there is no more data" is never mislabelled "we stopped asking". `"stalled"` and
`"max_pages"` rank last for the same reason: they mean the walk gave up on a
source that would not end, so any reason the source or the caller supplied is the
truer answer.

`"cycle"`, `"stalled"` and `"max_pages"` are the walk's three floors against a
source that never finishes. Nothing else bounds one: the App Store RSS feed has
its own page ceiling, but Connect and both Play sources rely on the endpoint to
stop issuing cursors.

- `"cycle"` catches a repeated page token.
- `"stalled"` catches the harder case, a *fresh* token every page with no reviews
  on it. `limit` and `since` are both driven by reviews actually seen, so an
  empty-page source escapes them both: `limit=5` would otherwise walk forever.
- `"max_pages"` is the backstop for a source that returns data forever, and it
  also bounds the cursor set the walk retains to detect cycles. Raise
  the public `max_pages=` argument on `fetch()`, `afetch()`, `iter_pages()`,
  `aiter_pages()`, `iter_reviews()`, or `aiter_reviews()` when a bounded job
  intentionally needs more pages.

---

## Country

`StrEnum` with two-letter country codes.

```python
from app_reviews import Country
```

### Region Groups

| Group | Description |
|-------|-------------|
| `Country.ALL` | All 155 supported countries. |
| `Country.EUROPE` | European countries. |
| `Country.AMERICAS` | North and South America. |
| `Country.ASIA_PACIFIC` | Asia-Pacific region. |
| `Country.MIDDLE_EAST` | Middle East and North Africa. |
| `Country.ENGLISH_SPEAKING` | English-speaking countries. |

Plain strings also work: `countries=["us", "gb"]`.

---

## Sort

Controls review order.

```python
from app_reviews import Sort
```

| Value | Description |
|-------|-------------|
| `Sort.NEWEST` | Most recent first (default). |
| `Sort.OLDEST` | Oldest first. |
| `Sort.RATING` | Highest rated first. |

---

## RetryConfig

HTTP retry and timeout settings.

```python
from app_reviews import RetryConfig
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_retries` | `int` | `3` | Maximum number of retries per request. |
| `backoff_factor` | `float` | `0.5` | Multiplier for wait time between retries. |
| `timeout` | `float` | `30.0` | Per-request timeout in seconds. |
| `retry_on` | `tuple[int, ...]` | `(500, 502, 503, 504, 429)` | Immutable HTTP status codes that trigger a retry. Any collection passed by the caller is copied to a tuple. |
| `max_backoff` | `float` | `60.0` | Ceiling on any single wait, in seconds. |

Waits follow `backoff_factor * 2**attempt`, capped at `max_backoff`. A server's
`Retry-After` header overrides that schedule when present, because it is the only party
that knows when it will serve again, and returning sooner than asked is what turns
throttling into a longer block. Both seconds (`Retry-After: 30`) and the HTTP-date
form are read, and both are capped at `max_backoff` too, so an outsized header
cannot park a request.

```python
from app_reviews import AppStoreReviews, RetryConfig

client = AppStoreReviews(
    retry=RetryConfig(max_retries=5, backoff_factor=1.0, retry_on=(429, 503))
)
```

---

## AppMetadata

Returned by `search()` and `lookup()` on the search clients.

```python
from app_reviews import AppStoreSearch

with AppStoreSearch() as client:
    metadata = client.lookup("123456789")   # AppMetadata | None
```

| Field | Type | Description |
|-------|------|-------------|
| `app_id` | `str` | App ID or package name. |
| `store` | `"appstore"` or `"googleplay"` | Which store. |
| `name` | `str` | App display name. |
| `developer` | `str` | Developer or publisher. |
| `category` | `str` | Primary category. |
| `price` | `str` | Store-formatted price, `"Free"`, or `"Unknown"`. |
| `version` | `str` | Current version, where the store publishes one. |
| `rating` | `float` | Average star rating. |
| `rating_count` | `int` | Total number of ratings. |
| `url` | `str` | Store page URL. |
| `current_version_release_date` | `datetime \| None` | When the current version shipped. |
| `first_release_date` | `datetime \| None` | When the app first appeared on the store. |

Text and number fields are non-optional, so a store that does not report one gets
a stated placeholder rather than `None`. The two dates are the exception: a date
has no honest placeholder, and a sentinel would sort, filter and diff as though it
were real. Precision differs by store: the App Store sends a real timestamp,
Google Play only the day it renders, so a Play date is midnight UTC on that day.
Measured against the live stores:

| field | `AppStoreSearch` | `GooglePlaySearch.lookup` | `GooglePlaySearch.search` |
|---|---|---|---|
| `name` / `developer` / `category` / `rating` | yes | yes | yes |
| `rating_count` | yes | yes | always `0` |
| `version` | yes | when the app publishes one | always `"Varies with device"` |
| `icon_url` | yes | yes | yes |
| `current_version_release_date` / `first_release_date` | yes, to the second | yes, to the day | always `None` |

- **Google Play publishes a version for some apps, not all.** `lookup()` returns
  the real one when the detail page carries it, and `"Varies with device"` when it
  does not, which is what the store itself shows for an app shipping per-device
  variants. Verified against the `us` storefront: Firefox publishes `153.0.1`,
  while Spotify and Duolingo publish nothing. A regular *search* hit has no version
  field at all, so it always reports the placeholder; use `lookup()` for a real
  one. Not to be confused with the version string attached to a *review*, which
  names the build that reviewer was running rather than the app's current release.
- **Play's search layout carries no rating count.** A regular search hit has a
  rating but no count anywhere in it, so `rating_count` is `0`. Two exceptions get
  a real count: `lookup()`, and the one *featured* hit a search returns, because
  Play embeds a full detail block for it. Use `lookup()` when you need counts.
- **`price` prefers the store's localized formatted price.** For Google Play,
  the following fallbacks are deterministic. Absent price data and an explicit
  numeric zero are `"Free"`. When no localized string is present, a positive
  numeric amount and a non-empty ISO currency code
  produce a conservative ISO fallback such as `"TRY 109.00"`; the client does
  not guess a currency symbol. Malformed or non-finite amounts are `"Unknown"`,
  as are positive amounts with a missing currency. Apple preserves its
  `formattedPrice` value and uses `"Unknown"` when that value is absent or
  unusable.

---

## Type Aliases

```python
from app_reviews.models.types import Store, Source
```

| Type | Values |
|------|--------|
| `Store` | `"appstore"`, `"googleplay"` |
| `Source` | `"appstore_scraper"`, `"appstore_official"`, `"googleplay_scraper"`, `"googleplay_official"` |

---
