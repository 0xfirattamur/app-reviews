---
description: Compare country behavior, ordering, history, and fields across four review sources.
---

# How the sources differ

Four sources back two stores, and they do not behave alike. This page is the
reference for the differences that change how you call the library.

There is no runtime object to query. A `capabilities()` function used to publish
these as data and was removed in 0.6.0: of its seven fields the library read two,
one had the same value for every source, and one described a reply API this
package never calls. "How do these sources differ" is a question you have while
*writing* code, so it is answered here, where you are at the time.

Which source you get is decided by whether you pass `auth=`:

| Client | Source |
|---|---|
| `AppStoreReviews()` | `appstore_scraper` |
| `AppStoreReviews(auth=...)` | `appstore_official` |
| `GooglePlayReviews()` | `googleplay_scraper` |
| `GooglePlayReviews(auth=...)` | `googleplay_official` |

`client.source` tells you at runtime. Country handling is part of each public
method's contract: Apple RSS accepts storefronts, App Store Connect is global,
and Google Play review clients reject country arguments.

| | `appstore_scraper` | `appstore_official` | `googleplay_scraper` | `googleplay_official` |
|---|---|---|---|---|
| Credentials | none | Connect `.p8` | none | service account |
| Countries | **per storefront** | global | unsupported (rejected) | unsupported (rejected) |
| Page order | newest-first | newest-first | newest-first | **not guaranteed** |
| History | unbounded | unbounded | unbounded | **last 7 days** |
| Ceiling | **~500 per storefront** | unbounded | unbounded | unbounded |
| `review.country` | set | set | `None` | `None` |
| `review.title` | set | set | `None` | legacy text only |
| `review.language` | `None` | `None` | `None` | set when reported |
| `review.raw` | populated | populated | populated | populated |

---

## Countries

Only `appstore_scraper` has a country dimension. Its feed URL is per storefront
(`https://itunes.apple.com/{country}/rss/...`), each request returns that
storefront's own reviews, and fetching `us` and `gb` gets you two different
review sets.

App Store Connect is global: one request covers every territory, and reviews
may carry their own territory. Its high-level client resolves a requested
storefront fan-out to one global walk.

Google Play review clients reject `country` and `countries` before network I/O.
Neither the public scraper nor the official Developer API exposes a review
country dimension, so silently accepting a storefront would suggest a filter
that does not exist. Google Play search and metadata still accept `country` to
select the storefront used for presentation, availability, and price.

`googleplay_scraper` looks like it should be per-country (the batchexecute
request does take `gl` and `hl`), and it is easy to assume varying them
partitions the corpus the way the RSS URL does. It does not. Play has one global
review corpus per app; `gl`/`hl` select the store's *presentation* locale
(currency formatting, translated UI strings), never a review filter. Fetching
`us`, `gb` and `de` against one app returns the same reviews three times. That is
also why `Review.country` is always `None` for this source: there is no
storefront to attribute a review to.

`appstore_official` is global in the same sense (one request), but each review
carries its own `territory`, so `Review.country` is set. Apple reports it as ISO
alpha-3 (`"USA"`); the package normalises to the alpha-2 form `Country` uses
(`"us"`), and the original stays in `raw["attributes"]["territory"]`.

## Page order

Three sources are guaranteed newest-first: the RSS feed asks for
`sortBy=mostRecent`, Connect for `sort=-createdDate`, and the Play scraper sends
Play's own "newest" sort id.

`googleplay_official` is not. The Developer API documents no ordering guarantee,
and truncating a fetch on an unverified assumption would silently lose reviews.

This gates the `since` early stop. On the three ordered sources, `since` stops
the walk once a page's oldest review predates it, so the later requests are never
made. Against `googleplay_official` every fetch walks to exhaustion and `since`
only filters what you get back.

## History

`googleplay_official` reaches back **seven days** and no further. Google
documents this in a `Note:` under "Retrieving a set of reviews" on the
[reply-to-reviews guide](https://developers.google.com/android-publisher/reply-to-reviews):

> Note: You can retrieve only the reviews that users have created or modified
> within the last week.

That is the API's own limitation, not a package restriction: the API refuses to
return anything older. For full Google Play history, export a CSV from Play
Console; this package does not read that format.

## Ceiling

`appstore_scraper` serves at most 10 pages of ~50 reviews per storefront, so
**~500 reviews per storefront** is a hard ceiling regardless of `since` or
`limit`. The walk reports `stopped_because="exhausted"` on reaching it, because the feed
genuinely offers nothing more.

The other three are unbounded in request terms, though `googleplay_official` is
bounded in time (above).

## `review.raw`

Every source populates it with the provider's own payload. That matters if you
keep an append-only observation log: a day fetched without `raw` can never be
reprocessed, so a mapping bug found later has nothing to go back to.

Prior to 0.6.0 the Play scraper set `raw=None` while the other three populated
it. Its shape differs by source: Apple and the official Play API send JSON
objects, while Play's web endpoint sends positional arrays, so `raw` is
`dict | list | None`.

`to_dicts()` omits it unless you ask: `result.to_dicts(include_raw=True)`. It is
off by default because the payload routinely dwarfs the review around it.

## Replies

Neither store's reply API is implemented here. If you build replies yourself,
note that they only accept identifiers the store's own list endpoint minted, so a
scraper-sourced review id will not work. This package does not verify that
claim, so treat it as a starting point rather than a fact.

---

### An empty App Store RSS feed is ambiguous

`appstore_scraper` cannot tell these three apart:

1. the app genuinely has no reviews
2. the app does not exist
3. Apple is throttling you

All three return HTTP 200 with a well-formed feed containing no entries, and the
responses are byte-identical, so the walk reports
`reviews=[], errors=[], stopped_because="exhausted"` in every case. Verified by
requesting a real app and a nonexistent one and getting the same 873-byte body.

This is a limit of the source, not something the package can classify: there is
nothing in the response to branch on. If "did this app return zero reviews, and
why" has to be answerable, use `appstore_official`: Connect answers 403 for an
app outside your account, which surfaces as `AuthError`.
