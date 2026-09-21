"""Tests for HTTP error classification."""

import pytest

from app_reviews.core.classify import (
    classify,
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
        assert classify(status) == expected

    def test_transport_error_wins_over_status(self):
        assert classify(0, transport_error="connection refused") == "transport"

    def test_status_zero_without_message_is_transport(self):
        assert classify(0) == "transport"

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
