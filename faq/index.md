# App review fetching FAQ

## How do I fetch App Store reviews in Python?

Use the numeric Apple app ID with `AppStoreReviews`; the public RSS source needs no credentials and is queried per storefront.

```
from app_reviews import AppStoreReviews, Country

with AppStoreReviews() as client:
    result = client.fetch(
        "324684580",
        countries=[Country.US],
        limit=50,
        max_pages=2,
    )
```

## How do I get Google Play reviews in Python?

Use the Android package name with `GooglePlayReviews`; the default public source needs no credentials.

```
from app_reviews import GooglePlayReviews

with GooglePlayReviews() as client:
    result = client.fetch("com.spotify.music", limit=50, max_pages=2)
```

## Do I need an API key or developer account?

No for the default public sources; yes for App Store Connect or the Google Play Developer API.

Pass `AppStoreAuth` or `GooglePlayAuth` to select the corresponding official source. Official APIs only expose apps available to the credentials.

## How many reviews can I fetch, and how far back?

It depends on the source: Apple RSS exposes about 500 recent reviews per storefront, while Google's Developer API exposes reviews created or modified in the last seven days.

App Store Connect and Google Play's public web source have no package-imposed history window, but upstream availability and throttling still apply. Set `limit=` for the number returned and `max_pages=` for the maximum requests per source walk.

## Is fetching App Store and Google Play reviews legal?

There is no universal answer; review the store terms, applicable law, and your use of personal data for the jurisdictions involved.

This project does not provide legal advice. Official APIs are preferable when you need store-supported access or account-scoped policy controls. Public sources can change or throttle access without notice.

## How is app-reviews different from google-play-scraper and app-store-scraper?

`app-reviews` covers both stores with one typed sync/async model and can also use both official review APIs.

The other projects are focused public-source clients with their own dictionary schemas. See the factual [comparison](https://0xfirattamur.github.io/app-reviews/comparison/index.md) before migrating.

## Why are title and country None on Google Play reviews?

Google Play's supported review sources do not report reviewer country, and its public web source does not have a review-title field.

The Developer API may carry a legacy title inside review text and may report `reviewerLanguage`; language is not location. Storefront parameters can affect presentation and pricing but do not partition the review corpus.

An explicit empty or all-blank `countries` collection is a no-op and makes no requests on every review client. Otherwise, Google Play review clients reject any nonblank `country` or `countries` selection before network I/O. Google Play search and metadata still accept `country` to select the storefront used for presentation, availability, and price.

## How do I fetch many Apple storefronts without running out of memory?

Use `iter_reviews()` to process reviews as pages arrive and set `max_pages`; the buffered `fetch()` path caps its default country fan-out at eight workers.

```
from app_reviews import AppStoreReviews, Country

with AppStoreReviews() as client:
    for review in client.iter_reviews(
        "324684580",
        countries=Country.ALL,
        max_pages=2,
    ):
        save(review.to_dict())
```

The streaming iterator moves through countries sequentially and does not return a `FetchResult`; page failures are logged. Use buffered `fetch()` when your automation must receive structured outcomes and errors.

## How should an agent consume a fetch result?

Return `FetchResult.to_dict()`, not only `to_dicts()`, so empty success, partial success, skipped records, and provider failure remain distinguishable.

```
payload = result.to_dict()
if payload["errors"]:
    handle_errors(payload["errors"])
```
