from app_reviews import Country
from app_reviews.models.result import CountryOutcome, FetchError, FetchResult
from tests.app_reviews.e2e.probes import (
    AppStoreProbe,
    ProbeObservation,
    first_non_empty,
    format_observations,
)
from tests.app_reviews.factories import make_review


def _outcome(*, error: FetchError | None = None) -> CountryOutcome:
    return CountryOutcome(
        country="us",
        pages=1,
        reviews_fetched=0,
        stopped_because="error" if error else "exhausted",
        elapsed=0.25,
        error=error,
    )


def _observation(result: FetchResult) -> ProbeObservation:
    return ProbeObservation(
        probe=AppStoreProbe("Example", "12345", Country.US),
        result=result,
    )


def test_formats_a_successful_empty_probe() -> None:
    observation = _observation(FetchResult(outcomes=[_outcome()]))

    assert observation.describe() == (
        "Example (12345, us): 0 reviews, pages=1, stop=exhausted, error=none"
    )


def test_formats_a_provider_error_with_kind_status_and_message() -> None:
    error = FetchError(
        country="us",
        message="HTTP 429 from the App Store RSS feed",
        kind="rate_limited",
        status=429,
    )
    observation = _observation(FetchResult(outcomes=[_outcome(error=error)]))

    assert observation.describe() == (
        "Example (12345, us): 0 reviews, pages=1, stop=error, "
        "error=rate_limited (HTTP 429): HTTP 429 from the App Store RSS feed"
    )


def test_selects_the_first_non_empty_observation() -> None:
    empty = _observation(FetchResult(outcomes=[_outcome()]))
    healthy = _observation(FetchResult(reviews=[make_review()], outcomes=[_outcome()]))

    assert first_non_empty([empty, healthy]) is healthy


def test_returns_none_when_every_probe_is_empty() -> None:
    observations = [
        _observation(FetchResult(outcomes=[_outcome()])),
        _observation(FetchResult(outcomes=[_outcome()])),
    ]

    assert first_non_empty(observations) is None


def test_formats_all_observations_for_one_failure_message() -> None:
    observations = [
        _observation(FetchResult(outcomes=[_outcome()])),
        _observation(FetchResult(outcomes=[_outcome()])),
    ]

    assert format_observations(observations) == (
        "- Example (12345, us): 0 reviews, pages=1, stop=exhausted, error=none\n"
        "- Example (12345, us): 0 reviews, pages=1, stop=exhausted, error=none"
    )
