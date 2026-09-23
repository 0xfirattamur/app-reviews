---
description: Bound review requests, resume cursors, and stream pages without buffering entire review corpora.
---

# Paging and cursors

`fetch()` walks every page for you. When you need to own the loop (to
checkpoint progress, to resume in another process, or to rate-limit yourself),
use the lower rungs.

If you only want the reviews and not the pages, skip to
[Streaming reviews](#streaming-reviews): `iter_reviews()` is the rung between
`iter_pages()` and `fetch()`, and it is what most streaming code wants.

---

## One page at a time

`fetch_page()` makes exactly one request and hands back the cursor:

```python
from app_reviews import AppStoreReviews

with AppStoreReviews() as client:
    page = client.fetch_page("324684580", country="us")
    print(len(page.reviews), page.next_cursor)
```

`next_cursor` is opaque and provider-specific: a page number for the RSS
feed, a URL for App Store Connect, a token for Google Play. Persist it
verbatim and pass it back to resume:

```python
with AppStoreReviews() as client:
    page = client.fetch_page("324684580", country="us", cursor=saved_cursor)
```

`next_cursor is None` means there are no more pages.

---

## Iterating one country

`iter_pages()` drives the loop and owns the `since` early stop:

```python
from datetime import datetime, timedelta, UTC

since = datetime.now(UTC) - timedelta(days=2)

for page in client.iter_pages("324684580", country="us", since=since):
    store(page.reviews)
    checkpoint(page.next_cursor)
```

The last page yielded carries `stopped_because`: `"exhausted"`, `"limit"`,
`"since"`, `"cycle"`, `"stalled"`, `"max_pages"` or `"error"`. Every earlier page
has `stopped_because is None`. See
[Models](../reference/models.md#stopreason) for what each value means and
[Source capabilities](../reference/capabilities.md) for which sources honor
`since`.

`iter_pages()` also accepts `limit`, which bounds *this walk*: it stops once
`limit` reviews have been yielded across pages.

```python
for page in client.iter_pages("324684580", country="us", limit=100):
    store(page.reviews)
```

`fetch()` does not always apply the same bound for the same `limit`. It
drives the identical page walk internally, but decides for itself whether
stopping at `limit` unfiltered reviews is safe, and exhausts pagination
instead when it is not: with a non-newest `sort`, on a source that does not
guarantee newest-first ordering, or when a `ratings`/`until` filter is also
requested, since none of those let it know in advance which of the first
`limit` reviews fetched will be the ones the caller actually wants. See
[Source capabilities](../reference/capabilities.md) for which sources
guarantee newest-first ordering.

---

## Streaming reviews

Reach for `iter_pages()` when you care about pages: cursors, checkpoints,
per-page errors. When you just want reviews, `iter_reviews()` yields them one at
a time and spans countries, so the nested loop disappears:

```python
for review in client.iter_reviews("324684580", countries=["us", "gb"]):
    handle(review)
```

versus the same thing a rung lower:

```python
for country in ["us", "gb"]:
    for page in client.iter_pages("324684580", country=country):
        for review in page.reviews:
            handle(review)
```

The difference from `fetch()` is memory, not convenience. `fetch()` filters,
sorts and limits across the whole corpus, so it must hold every review of every
country before it returns anything; with `Country.ALL` that is 155
storefronts at once. `iter_reviews()` holds one page.

That is also the trade: no cross-country sorting, and no `ratings` or `until`
filtering, because all three need the full set in hand. Reviews arrive in fetch
order, country by country, walked in sequence rather than concurrently, because a
concurrent fan-out would have to buffer to put results back in order, which is
the cost this rung exists to avoid.

`limit` here means "yield at most this many", counted across countries, not
`fetch()`'s "the N best under `sort`". `since` behaves as it does in
`iter_pages()`, stopping the walk early where the source guarantees ordering.

Because a generator has nowhere to hand a `FetchError` back, a country whose
walk fails is logged at WARNING and skipped. Use `fetch()` or `iter_pages()`
when you need the failure as data.

`aiter_reviews()` is the async twin.

---

## Errors

`iter_pages()` reports failures rather than raising: a failed page is yielded
with `.error` set and the walk stops, so you keep the pages you already
consumed. The one exception is `AuthError`: an unusable credential is
configuration that every country and page would repeat, so it is raised rather
than reported N times:

```python
for page in client.iter_pages("324684580", country="us"):
    if page.error:
        if page.error.retryable:
            schedule_retry(page.error)
        break
    store(page.reviews)
```

See [Models](../reference/models.md#fetcherror) for `ErrorKind`.
