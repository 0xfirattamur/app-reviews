# Async

Every entry point has an async twin using real async I/O over `httpx`, not a thread-pool wrapper.

| sync               | async                     |
| ------------------ | ------------------------- |
| `fetch()`          | `afetch()`                |
| `iter_reviews()`   | `aiter_reviews()`         |
| `iter_pages()`     | `aiter_pages()`           |
| `fetch_page()`     | `afetch_page()`           |
| `search()`         | `asearch()`               |
| `lookup()`         | `alookup()`               |
| `close()` / `with` | `aclose()` / `async with` |

```
import asyncio

from app_reviews import AppStoreReviews, Country


async def main():
    async with AppStoreReviews() as client:
        result = await client.afetch(
            "324684580", countries=[Country.US, Country.GB]
        )
    for outcome in result.outcomes:
        print(outcome.country, outcome.reviews_fetched, outcome.stopped_because)


asyncio.run(main())
```

`afetch()` fans out across countries with `asyncio.gather` behind a semaphore, with no threads. `concurrency=1` makes it sequential.

```
async def walk_pages(client):
    async for page in client.aiter_pages("324684580", country="us"):
        await store(page.reviews)
```

```
async def stream_reviews(client):
    async for review in client.aiter_reviews("324684580", countries=["us", "gb"]):
        await handle(review)
```

The sync path is a separate implementation, not a wrapper around the async one, so calling `fetch()` from inside a running event loop is safe.

Async credential work stays off the event loop: the Google Play token exchange is awaited, and App Store JWT signing, which is blocking local work with no async equivalent, runs in a thread. Either way it happens once per client, not once per request.

______________________________________________________________________

## Closing the pool

Each client owns one connection pool, so its sockets outlive a single request. Use `async with`, or call `aclose()`:

```
async def main():
    async with AppStoreReviews() as client:
        result = await client.afetch("324684580")
```

______________________________________________________________________

## Async search and lookup

`AppStoreSearch` and `GooglePlaySearch` follow the same pattern:

```
from app_reviews import AppStoreSearch


async def main():
    async with AppStoreSearch() as client:
        results = await client.asearch("fitness tracker")
        app = await client.alookup("com.burbn.instagram")
```

## Async metadata lookup

```
from app_reviews import AppStoreSearch


async def main():
    async with AppStoreSearch() as client:
        metadata = await client.alookup("123456789")
```
