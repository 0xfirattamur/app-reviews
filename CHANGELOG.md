# Changelog

All notable changes to `app-reviews` are recorded here. Release details for
recent versions are also kept in
[`.github/release-notes`](https://github.com/0xfirattamur/app-reviews/tree/main/.github/release-notes).

## [1.2.0] - 2026-09-28

Reply to reviews on both stores, list App Store versions from App Store Connect,
and pass credentials without a key file. Backward compatible.

### Added

- `AppStoreAuth(key_id, issuer_id, key_path=None, private_key=None)` takes the
  `.p8` either as a path or as PEM text, and `GooglePlayAuth(
  service_account_path=None, service_account_info=None)` takes the service
  account either as a path or as the parsed JSON mapping. Each needs exactly one
  of its two sources (`ValueError` otherwise); neither shows the key in `repr`
  or in the frames of a validation error.
- `AppStoreReplies(auth)` with `reply()`, `get_reply()`, and `delete_reply()`,
  and `GooglePlayReplies(auth)` with `reply(..., package_name=)` and
  `get_reply(..., package_name=)`, each with an async twin. They create or
  replace, read, and (App Store only) delete the developer reply through App
  Store Connect `customerReviewResponses` and the Play Developer API
  `reviews.reply` / `reviews.get`.
- `ReviewReply(review_id, reply_id, text, state, updated_at)` and
  `ReplyState = Literal["published", "pending"]`. Apple can keep a reply
  `"pending"` for up to 24 hours.
- `ReplyRejectedError(reason)`, a `RequestError`: the store refused the reply,
  or a Play reply was over 350 characters (`reason="too_long"`, raised before
  sending). `ReplyOutcomeUnknownError`, an `HttpError`: a write timed out, lost
  its connection, or got a 5xx, so it may or may not have taken effect.
- `RateLimitError.retry_after`, the wait in seconds a 429 asked for, on reads and
  writes alike.
- `AppStoreVersions(auth).versions(app_id)` / `aversions()`, returning
  `list[AppStoreVersion]` from App Store Connect `appStoreVersions`, with
  `whatsNew` per locale in `release_notes`. The official API has no release
  date: `created_at` (`createdDate`) and `earliest_release_date`
  (`earliestReleaseDate`) are named for what they are, `state` is
  `appVersionState`, and `AppStoreSearch.version_history()` remains the source
  for release dates.
- `HttpClient.send_once(method, url, body=, headers=)` / `asend_once()`: one
  attempt, never retried and never redirected, for a request that must not be
  applied twice. `HttpResponse.retry_after` carries the final attempt's
  `Retry-After`.

### Changed

- Reply writes are never retried, whatever `RetryConfig` says, and never follow
  a redirect, since following a 307/308 re-sends the write; a 3xx raises
  `ReplyOutcomeUnknownError`. Reads, token exchanges, and every other request
  keep the normal retry policy.
- A 429 from the Google token exchange now carries `RateLimitError.retry_after`.
- `GoogleAuth` also takes `service_account_info=`; `service_account_path` (by
  position or keyword) works as before, and exactly one of the two is required.

See the [v1.2.0 release notes](https://github.com/0xfirattamur/app-reviews/blob/main/.github/release-notes/v1.2.0.md).

## [1.1.0] - 2026-09-28

A shared rate limit for processes that fetch many apps from one address, App
Store release history with exact dates, and App Store reviews read from the XML
feed when the JSON feed comes back empty. Backward compatible except for one
reclassification: an App Store RSS 403 is now a retryable `rate_limited`
failure instead of `request` (see Changed). The XML fallback also applies
without opting in. Otherwise, omitting the new parameters keeps 1.0.0 behavior.

### Added

- `RateLimiter(rate, burst=1, *, initial_penalty=30.0, max_penalty=900.0)`, a
  thread-safe and asyncio-safe token bucket one process can share across every
  client, thread, and task. `penalize(seconds)` pauses every holder.
- `rate_limiter=` on `HttpClient`, `AppStoreReviews`, `GooglePlayReviews`,
  `AppStoreSearch`, and `GooglePlaySearch`. Every attempt, retries included,
  takes a token. A 429, or a 403 from a credential-free request, pauses the
  limiter for `Retry-After`, else for 30 seconds doubling per consecutive
  throttled answer, capped at `max_penalty`; a success resets the doubling.
  Passing it alongside `http=` raises `TypeError`, like `proxy=` and `retry=`.
- `RequestLimiter`, the protocol `rate_limiter=` accepts: `acquire()`,
  `aacquire()`, and `record(status, retry_after)`, which `HttpClient` calls once
  per response (not for a transport failure, nor for a 403 on a credentialed
  request). `RateLimiter` implements it; any other object with those methods,
  such as a limiter shared across processes, can be passed instead.
- `AppStoreSearch.version_history(app_id, *, country="us")` and
  `aversion_history()`, returning the app's App Store "Version History" as
  `list[AppVersionEntry]`, newest first. Read from the public product page
  through the client's `HttpClient`, so `proxy=`, `retry=`, and `rate_limiter=`
  apply. An unknown app (HTTP 404) or a page without a history returns `[]`; a
  history that cannot be read raises `ParseError`. This is a scraped source:
  best-effort, App Store only, and it may break when Apple changes the page. An
  official source from App Store Connect is planned for 1.2.0.
- `AppVersionEntry(version, released_at, release_notes)`, a frozen dataclass
  exported from `app_reviews`. `released_at` is timezone-aware UTC, and
  `release_notes` is named like `AppMetadata.release_notes`.
- `AppMetadata.release_notes`, the current version's "What's New" text: from
  iTunes `releaseNotes` on every App Store result, and from the Google Play
  detail page on `lookup()` and the featured search hit. `None` when absent.
- `PageResult.feed_format` and `CountryOutcome.feed_format` (`"json"`, `"xml"`,
  or `None`), also in both `to_dict()` envelopes, and the `FeedFormat` type.
  They say which App Store RSS feed answered; `None` for other sources.

### Fixed

- App Store RSS pages the JSON feed answers 200 with no entries, or with an
  unreadable body, are asked again from the same page's XML (Atom) feed, and
  its entries are used when it has any. Duolingo, Spotify, Facebook, and
  Instagram storefronts have been seen answering an empty JSON page 1 while the
  XML page held 50 reviews, which 1.0.0 reported as an exhausted storefront.
  The XML request goes through the same `HttpClient`, so retry, proxy, and rate
  limiter apply. It is made on page 1, and on a later page unless the previous
  page was short. Both feeds empty is still a normal `"exhausted"`; an
  unreadable XML body leaves the JSON answer standing; a failed XML request is
  reported as the page's `FetchError`.

### Changed

- An App Store RSS 403 is now `FetchError(kind="rate_limited", retryable=True,
  status=403)` instead of a non-retryable `request` failure. The feed answers
  403 while it throttles an address. Credentialed endpoints keep 403 as `auth`,
  and the 403 is not retried inside the package.

See the [v1.1.0 release notes](https://github.com/0xfirattamur/app-reviews/blob/main/.github/release-notes/v1.1.0.md).

## [1.0.0] - 2026-09-22

The first stable API release.

### Added

- Complete `FetchResult.to_dict()` envelopes with reviews, outcomes, errors,
  retryability, stop reasons, skipped-record counts, and optional raw payloads.
- Explicit `max_pages` request budgets across buffered and streaming review
  fetches.
- Context management plus public protocol and transport types for typing,
  lower-level use, and direct built-in provider integration. Custom providers
  cannot be injected into the high-level clients or paging engine in v1.
- Root exports for `HttpResponse`, `ReviewProvider`, `TokenSource`, and the new
  non-retryable `RequestError` classification.
- Reviewer language and legacy-title mapping for Google Play's official API.

### Changed

- Review IDs are required. Naive timestamps get UTC attached; already-aware
  offsets are preserved.
- Date-only `until` filters include the complete UTC day.
- The default multi-storefront concurrency is capped at eight.
- Negative limits raise; zero limits and explicit empty or all-blank country
  collections make no requests.
- Google Play rejects any nonblank review-country selection before network I/O;
  its search and metadata storefront selector remains supported.
- Permanent HTTP client errors are non-retryable and RSS access blocks receive a
  source-appropriate classification.
- Google Play prices retain the storefront's formatted currency.
- Provider pages report malformed records instead of silently losing them.

### Security

- App Store Connect pagination cursors are bound to the expected review endpoint
  and app before a credential-bearing request is sent.

See the [v1.0.0 release notes](https://github.com/0xfirattamur/app-reviews/blob/main/.github/release-notes/v1.0.0.md) for migration
instructions.

## [0.6.0] - 2026-08-02

Introduced the page/cursor ladder, native async entry points, typed outcomes,
pooled connections, bounded retry behavior, and a redesigned public review API.
This was a breaking prerelease. See the
[v0.6.0 release notes](https://github.com/0xfirattamur/app-reviews/blob/main/.github/release-notes/v0.6.0.md).

## [0.5.0] - 2026-07-29

Removed the terminal UI and CLI, made the package library-focused, and corrected
review identifiers. See the
[v0.5.0 release notes](https://github.com/0xfirattamur/app-reviews/blob/main/.github/release-notes/v0.5.0.md).

## [0.4.0] - 2026-04-09

Added the earlier unified review/search API and expanded tests and packaging.

## [0.3.1] - 2026-04-08

Corrected prerelease packaging and provider behavior.

## [0.3.0] - 2026-04-08

Expanded store search and review fetching capabilities.

## [0.2.1] - 2026-04-07

Corrected metadata and distribution details.

## [0.2.0] - 2026-04-07

Added the first cross-store public API.

## [0.1.1] - 2026-04-04

Corrected initial package behavior and metadata.

## [0.1.0] - 2026-04-04

Initial release.

[1.2.0]: https://github.com/0xfirattamur/app-reviews/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/0xfirattamur/app-reviews/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/0xfirattamur/app-reviews/compare/v0.6.0...v1.0.0
[0.6.0]: https://github.com/0xfirattamur/app-reviews/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/0xfirattamur/app-reviews/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/0xfirattamur/app-reviews/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/0xfirattamur/app-reviews/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/0xfirattamur/app-reviews/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/0xfirattamur/app-reviews/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/0xfirattamur/app-reviews/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/0xfirattamur/app-reviews/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/0xfirattamur/app-reviews/releases/tag/v0.1.0
