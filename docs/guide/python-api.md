---
description: Public sync and async app-reviews Python API, result models, errors, search, and metadata.
---

# Python API

Four main classes: two for reviews, two for search and lookup. All follow the same pattern: create a client, call a method.

`fetch()` is the top of a four-rung ladder (`fetch_page()` -> `iter_pages()` -> `iter_reviews()` -> `fetch()`), and every rung has an async twin. This page covers `fetch()`; see [Paging and cursors](paging.md) for the lower rungs (including `iter_reviews()`, which streams reviews instead of buffering them), and [Async](async.md) for the `a`-prefixed equivalents.

Every client owns one HTTP connection pool, so close it when you are done or use it as a context manager. See [Connection pooling](#connection-pooling).

---

## Imports

Everything public comes from the top-level package. There is one import path per
name:

```python
from app_reviews import AppStoreReviews, Country, Sort, FetchResult, HttpError
```

That is the whole contract, and it is why the package's internal layout is free
to change without breaking you: nothing supported points at a submodule.
`app_reviews.models` deliberately re-exports nothing; import the models from the
root.

For annotating your own code, the closed vocabularies are exported too:

```python
from app_reviews import ErrorKind, Review, Sort, Source, StopReason, Store

def handle(review: Review, source: Source) -> None: ...
```

Two submodules are documented and reasonable to reach into:

```python
from app_reviews.appstore import AppStoreScraperProvider, AppStoreOfficialProvider
from app_reviews.googleplay import GooglePlayScraperProvider, GooglePlayOfficialProvider
```

`app_reviews.appstore` and `app_reviews.googleplay` each hold one store's five
pieces: credentials, its two providers, its reviews client, its search client.
Reach in when you want to drive a provider directly, or to pin a source rather
than letting the presence of credentials choose it. Both also re-export their two
clients, but prefer the root for those.

`app_reviews.core` is the store-agnostic engine: the connection pool, the page
walk, the protocols. Internal, and carries no compatibility promise.

---

## AppStoreReviews

```python
from app_reviews import AppStoreReviews, AppStoreAuth
```

### Constructor

```python
client = AppStoreReviews(
    auth=None,       # AppStoreAuth | None: credentials for App Store Connect API
    proxy=None,      # str | None: HTTP proxy URL
    retry=None,      # RetryConfig | None: retry settings
    http=None,       # HttpClient | None: supply your own connection pool
)
```

Without `auth`, uses the public RSS feed. With `auth`, uses the App Store Connect API.

### fetch()

```python
result = client.fetch(
    app_id,          # str: App Store ID (numeric)
    countries=None,  # Collection[Country | str] | None: storefronts (default: ["us"])
    since=None,      # date | datetime | None: only reviews on or after this date
    until=None,      # date | datetime | None: only reviews on or before this date
    ratings=None,    # list[int] | None: filter to specific star ratings
    sort=Sort.NEWEST,# Sort: sort order
    limit=None,      # int | None: max reviews to return
    concurrency=None,# int | None: max countries fetched in parallel (default: 8)
    max_pages=None,  # int | None: request budget per country (default: 10,000)
)
```

`since` also **reduces how many requests are made**, not just what is
returned: on a source whose pages arrive newest-first (see
[how the sources differ](../reference/capabilities.md#page-order)), the
page walk stops as soon as it reaches a page older than `since`. See
[How It Works](../reference/how-it-works.md#the-fetch-pipeline).

`limit` bounds the walk too, but only for `sort=Sort.NEWEST` on a
newest-first source; with `Sort.OLDEST` or `Sort.RATING` the "best N" are not
the first N fetched, so pagination must exhaust before truncating.

### AppStoreAuth

```python
auth = AppStoreAuth(
    key_id="ABC123DEF4",
    issuer_id="12345678-1234-1234-1234-123456789012",
    key_path="/path/to/AuthKey_ABC123DEF4.p8",
)
```

### Examples

```python
# No auth (public RSS feed)
client = AppStoreReviews()
result = client.fetch("123456789")

# Multiple countries
from app_reviews import Country
result = client.fetch("123456789", countries=[Country.US, Country.GB, Country.DE])

# With auth
client = AppStoreReviews(
    auth=AppStoreAuth(
        key_id="ABC123DEF4",
        issuer_id="12345678-1234-1234-1234-123456789012",
        key_path="/path/to/AuthKey.p8",
    )
)
result = client.fetch("123456789", countries=[Country.US, Country.GB])

# Reuse client
spotify = client.fetch("324684580", countries=[Country.US, Country.GB])
instagram = client.fetch("389801252", countries=[Country.US])
twitter = client.fetch("333903271", ratings=[1, 2])

# Filter by date and rating
from datetime import date
result = client.fetch("123456789", ratings=[1, 2], since=date(2025, 1, 1))
```

---

## GooglePlayReviews

```python
from app_reviews import GooglePlayReviews, GooglePlayAuth
```

### Constructor

```python
client = GooglePlayReviews(
    auth=None,       # GooglePlayAuth | None: credentials for Developer API
    proxy=None,      # str | None: HTTP proxy URL
    retry=None,      # RetryConfig | None: retry settings
    http=None,       # HttpClient | None: supply your own connection pool
)
```

Without `auth`, uses the public web endpoint. With `auth`, uses the Google Play Developer API.

### fetch()

Same parameters as `AppStoreReviews.fetch()`, except `app_id` is a package name (e.g. `"com.example.app"`).

### GooglePlayAuth

```python
auth = GooglePlayAuth(
    service_account_path="/path/to/service-account.json",
)
```

### Examples

```python
# No auth
client = GooglePlayReviews()
result = client.fetch("com.example.app")

# With auth
from app_reviews import Sort
client = GooglePlayReviews(
    auth=GooglePlayAuth(service_account_path="/path/to/service-account.json")
)
result = client.fetch("com.example.app", countries=[Country.US], sort=Sort.NEWEST, limit=100)
```

---

## Country Enum

`Country` is a `StrEnum`; values are two-letter country codes.

```python
from app_reviews import Country

Country.US   # "us"
Country.GB   # "gb"
Country.DE   # "de"
```

**Region groups:**

| Group | Description |
|-------|-------------|
| `Country.ALL` | All 155 supported countries |
| `Country.EUROPE` | European countries |
| `Country.AMERICAS` | North and South America |
| `Country.ASIA_PACIFIC` | Asia-Pacific region |
| `Country.MIDDLE_EAST` | Middle East and North Africa |
| `Country.ENGLISH_SPEAKING` | English-speaking countries |

Each group is a `frozenset[Country]` and can be passed straight to `countries=`,
which takes any collection of `Country` or `str`. Plain strings work too:
`countries=["us", "gb"]`. Entries are normalised and deduplicated, so `"US"`,
`"us"` and `"USA"` name one storefront and are walked once.

---

## Sort Enum

```python
from app_reviews import Sort

Sort.NEWEST   # most recent first (default)
Sort.OLDEST   # oldest first
Sort.RATING   # highest rated first
```

---

## Working with Results

### Iterate, count, and check

```python
result = client.fetch("123456789")

for review in result:
    print(review.title)

print(f"Reviews: {len(result)}")

if result:
    print("Got reviews!")
```

### Filter after fetching

```python
from datetime import date

bad_recent = result.filter(ratings=[1, 2], since=date(2025, 1, 1))
```

### Check errors

```python
if result.errors:
    for err in result.errors:
        print(f"Failed: {err.country} ({err.message})")
```

### Serialise

`to_dict()` gives you the complete JSON-safe result envelope, including errors,
outcomes and skipped-record counts. `to_dicts()` is the compatibility helper for
review rows only. Provider payloads (`raw`) are left out unless you ask for them:

```python
payload = result.to_dict()                     # reviews + diagnostics
records = result.to_dicts()                    # review rows only
records = result.to_dicts(include_raw=True)    # keep the provider payload
```

From there the standard library does the rest. The package ships no exporters:
serialisation is a solved problem and `json`/`csv` do it better than a wrapper
would.

```python
import csv, json

json.dumps(result.to_dicts(), indent=2)                      # JSON
"\n".join(json.dumps(d) for d in result.to_dicts())          # JSONL

rows = result.to_dicts()
if rows:
    with open("reviews.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
```

`newline=""` is required when writing CSV; without it, review bodies
containing newlines produce broken rows on some platforms.

### Review

`fetch()` returns a `FetchResult` containing `Review` objects, frozen dataclasses with these fields:

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` | Raw identifier assigned by the source ([details](../reference/models.md#review-ids)) |
| `store` | `Store` | `"appstore"` or `"googleplay"` |
| `app_id` | `str` | Numeric Apple app ID or Google Play package name |
| `country` | `str \| None` | Storefront queried. `None` if the source does not report one (e.g. `googleplay_official`, `googleplay_scraper`) |
| `rating` | `int` | Star rating (`1`-`5`) |
| `title` | `str \| None` | Review title. Google Play web has none; the official API may expose a legacy title |
| `body` | `str` | Review text |
| `author_name` | `str` | Reviewer display name |
| `app_version` | `str \| None` | App version at time of review |
| `created_at` | `datetime \| None` | When written. `None` on sources that only report a modification time |
| `updated_at` | `datetime \| None` | Last modified. `None` on sources that only report a creation time |
| `source` | `Source` | Provider (e.g. `"appstore_scraper"`, `"googleplay_official"`) |
| `language` | `str \| None` | Review language code |
| `fetched_at` | `datetime \| None` | When the review was fetched |
| `raw` | `dict \| list \| None` | Raw provider payload. Apple and official Play use objects; Play web uses arrays |

### Error handling

`fetch()` does not raise on partial failures. Check `result.errors`: each is
a `FetchError` with a typed `kind` (`ErrorKind`) to branch retry policy on,
rather than parsing exception text:

```python
result = client.fetch("123456789")

if not result and result.errors:
    print("All fetches failed:")
    for err in result.errors:
        print(f"  {err.country}: {err.message} ({err.kind})")
elif result.errors:
    print(f"Got {len(result)} reviews, but some countries failed:")
    for err in result.errors:
        if err.retryable:
            schedule_retry(err)
        print(f"  {err.country}: {err.message} ({err.kind})")
```

`fetch()` surfaces errors that older versions swallowed: previously, a page
error on a country that had already yielded some reviews was logged and
discarded, so a non-empty `result` could still hide a failure. Errors now
always reach `result.errors`, so code that treated "got some reviews" as
"nothing failed" will start seeing failures it did not see before.

`search()` and `lookup()` **raise** instead: a single
request has a single outcome, so there is no partial result to hand back. They
raise `HttpError`, which carries the same `kind` and `status` you would have got
from a `FetchError`:

```python
from app_reviews import AppStoreSearch, AuthError, HttpError, RateLimitError

try:
    apps = AppStoreSearch().search("fitness tracker")
except RateLimitError as err:
    back_off(err.status)
except AuthError:
    alert_a_human()
except HttpError as err:
    log(type(err).__name__, err.status)
```

The exception class carries the classification, so there is no `kind` attribute to
read and no `kind=` to pass, so an error cannot be labelled as something it is
not. `status` is the HTTP status when the failure came from a response, and `None`
when the exchange never produced one.

| exception | raised when | retry? |
|---|---|---|
| `RateLimitError` | HTTP 429 | yes, later |
| `ServerError` | HTTP 5xx | yes |
| `TransportError` | connection refused, timeout, or an exchange that did not complete | yes |
| `AuthError` | credentials rejected, or unusable; 401/403 from an official credentialed endpoint | **no** |
| `RequestError` | another permanent HTTP 4xx rejection, including 401/403 from a credential-free public endpoint | no |
| `NotFoundError` | HTTP 404 | no |
| `ParseError` | a success carrying an unreadable body | no |

All except `AuthError` subclass `HttpError`, so `except HttpError` still catches
every request-level failure. `AuthError` sits beside it because an unreadable key
file is an auth failure with no HTTP in it. Everything subclasses
`AppReviewsError`.

On the `fetch` path there is nothing to catch: a walk over many countries reports
`FetchError` values in `result.errors` instead, carrying the matching
[`ErrorKind`](../reference/models.md#errorkind) as data. One taxonomy, two
deliveries: `core.classify` maps a status to both.

---

## Connection pooling

Each client owns one `HttpClient`, which holds a single `httpx.Client` /
`AsyncClient` for its lifetime. That means a multi-page walk reuses one
connection instead of performing a TLS handshake per page, and that the sockets
stay open until you close them:

```python
with AppStoreReviews() as client:
    result = client.fetch("324684580")
```

`close()` and `aclose()` do the same thing explicitly. A client remains usable
afterwards; the next request reopens the pool.

To share one pool across several clients, or to set a custom transport, build it
yourself:

```python
from app_reviews import AppStoreReviews, AppStoreSearch, HttpClient, RetryConfig

pool = HttpClient(proxy="http://proxy.example.com:8080", retry=RetryConfig())
reviews = AppStoreReviews(http=pool)
search = AppStoreSearch(http=pool)
```

A pool you pass with `http=` already carries its own `proxy` and `retry`, so
passing either alongside it raises `TypeError` rather than silently ignoring
what you asked for.

### Per-country outcomes

`result.outcomes` is a `list[CountryOutcome]`, one per requested country (or a
single entry with `country=None` for global sources). Each reports how many
pages and reviews were fetched, why the walk stopped
(`stopped_because`), and how long it took:

```python
for outcome in result.outcomes:
    print(outcome.country, outcome.pages, outcome.reviews_fetched,
          outcome.stopped_because)
```

`stopped_because` distinguishes `"exhausted"` (no more data) from every other
value, all of which mean more data may exist: `"limit"` and `"since"` because the
walk stopped asking, `"cycle"`, `"stalled"` and `"max_pages"` because it gave up on
a source that would not end, and `"error"`.
See [Models](../reference/models.md#countryoutcome).

---

## App Search & Lookup

Search for apps by keyword and look up app metadata by ID. No authentication required.

### AppStoreSearch

```python
from app_reviews import AppStoreSearch, Country
```

```python
client = AppStoreSearch(
    proxy=None,      # str | None: HTTP proxy URL
    retry=None,      # RetryConfig | None: retry settings
    http=None,       # HttpClient | None: supply your own connection pool
)
```

#### search()

```python
results = client.search(
    "fitness tracker",       # str: search query
    country=Country.US,      # Country: store region (default: US)
    limit=50,                # int: max results (default: 50)
)
# returns list[AppMetadata]
```

#### lookup()

```python
app = client.lookup(
    "com.whatsapp.WhatsApp", # str: bundle ID
    country=Country.US,      # Country: store region (default: US)
)
# returns AppMetadata | None
```

### GooglePlaySearch

```python
from app_reviews import GooglePlaySearch, Country
```

```python
client = GooglePlaySearch(
    proxy=None,      # str | None: HTTP proxy URL
    retry=None,      # RetryConfig | None: retry settings
    http=None,       # HttpClient | None: supply your own connection pool
)
```

Same `search()` and `lookup()` methods as `AppStoreSearch`. For lookup, pass a package name (e.g. `"com.whatsapp"`).

### AppMetadata

Both `search()` and `lookup()` return `AppMetadata`, a frozen dataclass with these fields:

| Field | Type | Description |
|-------|------|-------------|
| `app_id` | `str` | Numeric track ID for App Store results, or Google Play package name |
| `store` | `Store` | `"appstore"` or `"googleplay"` |
| `name` | `str` | App display name |
| `developer` | `str` | Developer or publisher name |
| `category` | `str` | Primary category (e.g. `"Social Networking"`) |
| `price` | `str` | Localized store price, ISO fallback, `"Free"`, or `"Unknown"` |
| `version` | `str` | Current version string |
| `rating` | `float` | Average star rating (`0.0`-`5.0`) |
| `rating_count` | `int` | Total number of ratings |
| `url` | `str` | Store page URL |
| `icon_url` | `str \| None` | App icon image URL |
| `current_version_release_date` | `datetime \| None` | When the current version shipped |
| `first_release_date` | `datetime \| None` | When the app first appeared on the store |

> **Dates:** both are `None` when the store publishes none, because a date has no
> honest placeholder. Precision differs: the App Store sends a real timestamp,
> while Google Play publishes only a day, so a Play date is midnight UTC on that
> day. A Play *search* hit carries neither; use `lookup()`.

> **Note:** Google Play search results may have `"Unknown"` for `name`,
> `developer` and `category`, and `0` for `rating_count`, because a regular search hit
> carries no count. Google Play `price` prefers the localized display value;
> absent data or numeric zero becomes `"Free"`, a positive numeric amount plus
> ISO currency becomes an ISO-formatted fallback, and malformed/non-finite data
> or a missing currency becomes `"Unknown"`. `version` is always
> `"Varies with device"`, because a regular search hit carries no version field.
> Use `lookup()` for a real rating count, and for the real version when the app
> publishes one.

### Examples

```python
from app_reviews import AppStoreSearch, GooglePlaySearch, Country

# Search App Store
results = AppStoreSearch().search("weather", country=Country.GB, limit=5)
for app in results:
    print(f"{app.name} by {app.developer} ({app.rating}*)")

# Search Google Play
results = GooglePlaySearch().search("weather", country=Country.US, limit=5)
for app in results:
    print(f"{app.name}: {app.icon_url}")

# Look up a specific app, then fetch its reviews
from app_reviews import GooglePlayReviews
app = GooglePlaySearch().lookup("com.whatsapp")
if app:
    reviews = GooglePlayReviews().fetch(app.app_id, countries=[Country.US])
    print(f"{app.name}: {len(reviews)} reviews")
```

---

## Metadata for one app

Use the search client for the store you are asking about:

```python
from app_reviews import AppStoreSearch, GooglePlaySearch, Country

meta = AppStoreSearch().lookup("324684580")                  # None if absent
meta = GooglePlaySearch().lookup("com.whatsapp")
meta = AppStoreSearch().lookup("324684580", country=Country.DE)
```

`lookup()` returns `AppMetadata | None`, and `alookup()` is the async twin.

Earlier versions shipped a `lookup_metadata()` helper that guessed the store from
the id's shape. It was removed in 0.6.0: the guess was a lowercase reverse-DNS
regex, so real Play packages like `com.Slack` and `com.t_mobile.pr.mytmobile`
were routed to the App Store and came back as "not found". A caller always knows
which store an id came from, and if you genuinely need to route a mixed batch,
the test is one line, because App Store ids are numeric:

```python
from app_reviews import AppStoreSearch, GooglePlaySearch, HttpClient

with HttpClient() as pool:                       # one pool for the whole batch
    apple, play = AppStoreSearch(http=pool), GooglePlaySearch(http=pool)
    for app_id in app_ids:
        client = apple if app_id.isdigit() else play
        meta = client.lookup(app_id)
```
