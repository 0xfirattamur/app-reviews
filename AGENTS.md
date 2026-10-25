# AGENTS.md

Read [CONTRIBUTING.md](CONTRIBUTING.md) first: setup, `make` targets, and the
project standards live there. This file only adds what the code does not show.

## Changing code

- **Twins move together.** Every network entry point has a sync and an async
  version (`fetch`/`afetch`, `iter_reviews`/`aiter_reviews`, ...). Change both
  in the same commit and test both.
- **Stay compatible within 1.x.** New parameters are keyword-only with a default
  that keeps today's behaviour; new dataclass fields use
  `field(default=..., kw_only=True)`. Renaming or removing a public name from
  `app_reviews/__init__.py` is a breaking change.
- **Credentials stay out of frames.** Keys and service-account JSON must never
  appear in a `repr`, an exception message, or a traceback frame.
  `tests/app_reviews/test_credential_hygiene.py` checks this; keep it green when
  touching `appstore/auth.py`, `googleplay/auth.py`, or `models/config.py`.
- **Store payloads are untrusted.** A malformed response becomes a classified
  error (`ParseError` or `PageResult.error`), never an unhandled exception and
  never a silent empty success.

## Testing

- `make all` before handing back. Live tests hit real stores and run only with
  `-m live`; use them to confirm a store-side assumption, not as a regression
  test.
- Fixtures under `tests/fixtures` are recorded store responses. Strip any
  credential or personal review data before adding one.

## Changelog and releases

- Every user-visible change gets an entry under `## Unreleased` in
  `CHANGELOG.md`.
- Release preparation goes through `./scripts/release.sh --create X.Y.Z` only;
  the release workflow verifies what that script writes.
