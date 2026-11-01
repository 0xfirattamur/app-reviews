"""Tests for HTTP error classification."""

import pytest

from app_reviews.core.classify import (
    THROTTLE_STATUSES,
    classify,
    describe_failure,
    error_for,
    fetch_error_from_response,
    raise_for_http_failure,
)
from app_reviews.core.http import HttpResponse
from app_reviews.errors import RequestError
from app_reviews.models.result import FetchError


class TestClassify:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, "rate_limited"),
            (401, "auth"),
            (403, "auth"),
            (404, "not_found"),
            (500, "server"),
            (502, "server"),
            (503, "server"),
            (599, "server"),
            (400, "request"),
            (405, "request"),
            (409, "request"),
            (418, "request"),
            (422, "request"),
        ],
    )
    def test_maps_status_to_kind(self, status, expected):
        assert classify(status, credentialed=True) == expected

    def test_transport_error_wins_over_status(self):
        assert (
            classify(0, transport_error="connection refused", credentialed=True)
            == "transport"
        )

    def test_status_zero_without_message_is_transport(self):
        assert classify(0, credentialed=True) == "transport"

    @pytest.mark.parametrize("status", [401, 403])
    def test_credential_free_access_denial_is_a_request_error_on_both_paths(
        self, status
    ):
        assert classify(status, credentialed=False) == "request"
        assert error_for(status, credentialed=False) is RequestError
        assert (
            fetch_error_from_response(
                country="us",
                status=status,
                message="blocked",
                credentialed=False,
            ).kind
            == "request"
        )
        with pytest.raises(RequestError):
            raise_for_http_failure(
                HttpResponse(status=status, body=""),
                "public endpoint",
                credentialed=False,
            )


class TestDescribeFailure:
    @pytest.mark.parametrize(
        ("response", "throttled", "expected"),
        [
            (HttpResponse(status=200, body=""), (), None),
            (HttpResponse(status=0, body="", transport_error="boom"), (), "boom"),
            (HttpResponse(status=503, body=""), (), "HTTP 503 from the feed"),
            (
                HttpResponse(status=403, body=""),
                (403,),
                "HTTP 403 from the feed; access may be blocked or throttled",
            ),
            (HttpResponse(status=403, body=""), (), "HTTP 403 from the feed"),
        ],
    )
    def test_names_the_failure_or_nothing(self, response, throttled, expected):
        assert (
            describe_failure(response, "the feed", rate_limited_statuses=throttled)
            == expected
        )

    def test_throttle_statuses_are_the_ones_a_limiter_backs_off_on(self):
        assert frozenset({403, 429}) == THROTTLE_STATUSES


class TestRetryable:
    """Read off ``FetchError``, which is the only surface that publishes this.

    A free ``is_retryable(kind)`` in ``classify`` said the same thing and had no
    callers, so the fact now has one home.
    """

    def _error(self, kind):
        return FetchError(country="us", message="boom", kind=kind)

    @pytest.mark.parametrize("kind", ["rate_limited", "server", "transport"])
    def test_retryable_kinds(self, kind):
        assert self._error(kind).retryable is True

    @pytest.mark.parametrize("kind", ["auth", "not_found", "parse", "request"])
    def test_terminal_kinds(self, kind):
        assert self._error(kind).retryable is False
