# Changelog

All notable changes to `app-reviews` are recorded here. Release details for recent versions are also kept in [`.github/release-notes`](https://github.com/0xfirattamur/app-reviews/tree/main/.github/release-notes).

## [1.0.0](https://github.com/0xfirattamur/app-reviews/compare/v0.6.0...v1.0.0) - 2026-09-22

The first stable API release.

### Added

- Complete `FetchResult.to_dict()` envelopes with reviews, outcomes, errors, retryability, stop reasons, skipped-record counts, and optional raw payloads.
- Explicit `max_pages` request budgets across buffered and streaming review fetches.
- Context management plus public protocol and transport types for typing, lower-level use, and direct built-in provider integration. Custom providers cannot be injected into the high-level clients or paging engine in v1.
- Root exports for `HttpResponse`, `ReviewProvider`, `TokenSource`, and the new non-retryable `RequestError` classification.
- Reviewer language and legacy-title mapping for Google Play's official API.

### Changed

- Review IDs are required. Naive timestamps get UTC attached; already-aware offsets are preserved.
- Date-only `until` filters include the complete UTC day.
- The default multi-storefront concurrency is capped at eight.
- Negative limits raise; zero limits and explicit empty or all-blank country collections make no requests.
- Google Play rejects any nonblank review-country selection before network I/O; its search and metadata storefront selector remains supported.
- Permanent HTTP client errors are non-retryable and RSS access blocks receive a source-appropriate classification.
- Google Play prices retain the storefront's formatted currency.
- Provider pages report malformed records instead of silently losing them.

### Security

- App Store Connect pagination cursors are bound to the expected review endpoint and app before a credential-bearing request is sent.

See the [v1.0.0 release notes](https://github.com/0xfirattamur/app-reviews/blob/main/.github/release-notes/v1.0.0.md) for migration instructions.

## [0.6.0](https://github.com/0xfirattamur/app-reviews/compare/v0.5.0...v0.6.0) - 2026-08-02

Introduced the page/cursor ladder, native async entry points, typed outcomes, pooled connections, bounded retry behavior, and a redesigned public review API. This was a breaking prerelease. See the [v0.6.0 release notes](https://github.com/0xfirattamur/app-reviews/blob/main/.github/release-notes/v0.6.0.md).

## [0.5.0](https://github.com/0xfirattamur/app-reviews/compare/v0.4.0...v0.5.0) - 2026-07-29

Removed the terminal UI and CLI, made the package library-focused, and corrected review identifiers. See the [v0.5.0 release notes](https://github.com/0xfirattamur/app-reviews/blob/main/.github/release-notes/v0.5.0.md).

## [0.4.0](https://github.com/0xfirattamur/app-reviews/compare/v0.3.1...v0.4.0) - 2026-04-09

Added the earlier unified review/search API and expanded tests and packaging.

## [0.3.1](https://github.com/0xfirattamur/app-reviews/compare/v0.3.0...v0.3.1) - 2026-04-08

Corrected prerelease packaging and provider behavior.

## [0.3.0](https://github.com/0xfirattamur/app-reviews/compare/v0.2.1...v0.3.0) - 2026-04-08

Expanded store search and review fetching capabilities.

## [0.2.1](https://github.com/0xfirattamur/app-reviews/compare/v0.2.0...v0.2.1) - 2026-04-07

Corrected metadata and distribution details.

## [0.2.0](https://github.com/0xfirattamur/app-reviews/compare/v0.1.1...v0.2.0) - 2026-04-07

Added the first cross-store public API.

## [0.1.1](https://github.com/0xfirattamur/app-reviews/compare/v0.1.0...v0.1.1) - 2026-04-04

Corrected initial package behavior and metadata.

## [0.1.0](https://github.com/0xfirattamur/app-reviews/releases/tag/v0.1.0) - 2026-04-04

Initial release.
