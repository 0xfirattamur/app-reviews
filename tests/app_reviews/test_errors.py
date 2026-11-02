"""The exception hierarchy.

``AuthError`` used to be a bare subclass with no ``status`` while ``HttpError`` had
one, and the Google token exchange raised a bare ``AuthError`` for a dead
connection, a revoked credential and a malformed response alike, so a caller
either retried permanently broken credentials or abandoned a working account over
one timeout.

The class is now the classification: no ``kind`` attribute, no ``kind=`` argument,
so a mislabelled error cannot be constructed. ``error_for`` ties the raised
taxonomy to the ``ErrorKind`` that ``FetchError`` reports as data, and the tests
here pin the two together through it.
"""

from typing import get_args

import pytest

from app_reviews import (
    AppReviewsError,
    AuthError,
    ErrorKind,
    HttpError,
    NotFoundError,
    ParseError,
    RateLimitError,
    RequestError,
    ServerError,
    TransportError,
)
from app_reviews.core.classify import classify, error_for
from app_reviews.models.types import RETRYABLE_KINDS

LEAVES = (
    RateLimitError,
    AuthError,
    NotFoundError,
    ServerError,
    TransportError,
    ParseError,
    RequestError,
)

# The exception class each ``ErrorKind`` is raised as. Written out here so a new
# kind fails ``test_every_kind_has_exactly_one_class`` until it is given one.
CLASS_FOR_KIND = {
    "rate_limited": RateLimitError,
    "auth": AuthError,
    "not_found": NotFoundError,
    "request": RequestError,
    "server": ServerError,
    "transport": TransportError,
    "parse": ParseError,
}


def test_request_error_is_a_public_non_retryable_http_error():
    assert issubclass(RequestError, HttpError)
    assert RequestError not in TestRetryabilityIsReadableFromTheType.RETRYABLE


class TestTheClassIsTheClassification:
    @pytest.mark.parametrize("cls", LEAVES)
    def test_status_is_readable_even_when_unset(self, cls):
        assert cls("boom").status is None

    @pytest.mark.parametrize("cls", LEAVES)
    def test_status_survives_catching_the_base(self, cls):
        try:
            raise cls("boom", status=503)
        except AppReviewsError as err:
            assert err.status == 503


class TestHierarchy:
    @pytest.mark.parametrize("cls", LEAVES)
    def test_everything_is_an_app_reviews_error(self, cls):
        assert issubclass(cls, AppReviewsError)

    @pytest.mark.parametrize(
        "cls",
        (
            RateLimitError,
            NotFoundError,
            RequestError,
            ServerError,
            TransportError,
            ParseError,
        ),
    )
    def test_store_request_failures_are_http_errors(self, cls):
        """`except HttpError` keeps catching everything a request can raise."""
        assert issubclass(cls, HttpError)

    def test_auth_is_not_an_http_error(self):
        """Every AuthError today comes from a token exchange, but an unreadable
        .p8 on disk is an auth failure with no HTTP in it, so the is-a would be
        wrong the moment the App Store side starts raising."""
        assert not issubclass(AuthError, HttpError)


class TestTheTwoTaxonomiesCannotDrift:
    def test_every_kind_has_exactly_one_class(self):
        assert set(CLASS_FOR_KIND) == set(get_args(ErrorKind))
        assert len(set(CLASS_FOR_KIND.values())) == len(CLASS_FOR_KIND)

    def test_the_classes_are_exactly_the_leaves(self):
        assert set(CLASS_FOR_KIND.values()) == set(LEAVES)

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, RateLimitError),
            (401, AuthError),
            (403, AuthError),
            (404, NotFoundError),
            (400, RequestError),
            (422, RequestError),
            (500, ServerError),
            (503, ServerError),
        ],
    )
    def test_error_for_maps_status_to_class(self, status, expected):
        assert error_for(status, credentialed=True) is expected

    def test_error_for_prefers_a_transport_failure_over_the_status(self):
        assert error_for(0, "connection refused", credentialed=True) is TransportError

    @pytest.mark.parametrize("status", (400, 401, 403, 404, 422, 429, 500, 503, 0))
    def test_error_for_and_classify_agree(self, status):
        """The raised class and the returned string describe the same failure."""
        assert (
            error_for(status, credentialed=True)
            is CLASS_FOR_KIND[classify(status, credentialed=True)]
        )


class TestRetryabilityIsReadableFromTheType:
    RETRYABLE = (RateLimitError, ServerError, TransportError)

    @pytest.mark.parametrize("cls", LEAVES)
    def test_the_retryable_classes_are_the_retryable_kinds(self, cls):
        """No `retryable` flag on the exception to disagree with the type; the
        same RETRYABLE_KINDS drives FetchError.retryable."""
        kind = next(
            candidate
            for candidate, error_class in CLASS_FOR_KIND.items()
            if error_class is cls
        )

        assert (kind in RETRYABLE_KINDS) is (cls in self.RETRYABLE)
