# Contributing

Thanks for helping out. Bug reports, fixes, and new store support are all welcome.

Participation is covered by the [Code of Conduct](https://github.com/0xfirattamur/app-reviews/blob/main/CODE_OF_CONDUCT.md).

## Setup

```
git clone https://github.com/0xfirattamur/app-reviews.git
cd app-reviews
make install
pre-commit install
```

## Commands

| Command           | What it does                                             |
| ----------------- | -------------------------------------------------------- |
| `make all`        | lint + typecheck + test + build; run this before pushing |
| `make test`       | tests with coverage                                      |
| `make format`     | auto-fix style                                           |
| `make docs-serve` | preview docs at `localhost:8000`                         |

Live tests hit real store endpoints and are skipped by default. Run them with `make test ARGS="-m live"` or `uv run pytest -m live`.

## Standards

- Python 3.11+, `mypy --strict`, and ruff must all pass
- All HTTP goes through the shared `HttpClient` in `app_reviews.core.http`
- `cryptography` and `httpx` are the only runtime dependencies; keep it that way
- Models are `@dataclass(frozen=True, slots=True)`
- Coverage stays at or above 85%

## Pull requests

Branch from `main`, add tests, run `make all`, and open the PR. For anything large, open an issue first so the interface can be discussed. Small fixes can go straight to a PR. Never include store credentials or personal review data in a fixture or report.

## Releases

Releases are prepared on an exact `release/vX.Y.Z` branch and reviewed through a pull request to `main`:

```
git switch develop-or-another-reviewed-base
./scripts/release.sh --create X.Y.Z
# Finish release notes and changelog, then run:
make all
```

The preparation script refuses to run on `main` or a dirty tree. It updates the project, lockfile, citation, release-note template, and changelog, but never commits, tags, pushes, merges, or publishes. Commit the prepared changes on the release branch and open a PR. Only after that PR is reviewed and merged to `main` should a maintainer create and push `vX.Y.Z`; the tag workflow verifies the version, ancestry, tests, archives, and wheel before publishing.

## Security

Please don't file public issues for vulnerabilities. See the [security policy](https://github.com/0xfirattamur/app-reviews/blob/main/SECURITY.md).
