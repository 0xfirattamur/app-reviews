"""Tests for RetryConfig validation."""

import pytest

from app_reviews.models.config import RetryConfig


class TestRetryConfigValidation:
    def test_defaults_are_valid(self):
        config = RetryConfig()
        assert config.max_retries == 3
        assert config.backoff_factor == 0.5
        assert config.timeout == 30.0
        assert config.retry_on == (500, 502, 503, 504, 429)

    def test_max_retries_zero_is_valid(self):
        config = RetryConfig(max_retries=0)
        assert config.max_retries == 0

    def test_negative_max_retries_raises(self):
        with pytest.raises(ValueError, match="max_retries"):
            RetryConfig(max_retries=-1)

    def test_negative_backoff_factor_raises(self):
        with pytest.raises(ValueError, match="backoff_factor"):
            RetryConfig(backoff_factor=-0.1)

    def test_zero_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout"):
            RetryConfig(timeout=0)

    def test_negative_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout"):
            RetryConfig(timeout=-5.0)

    def test_custom_retry_on_list(self):
        config = RetryConfig(retry_on=[500, 429])
        assert config.retry_on == (500, 429)

    def test_retry_statuses_are_deeply_immutable(self):
        supplied = [500, 429]
        config = RetryConfig(retry_on=supplied)

        supplied.append(503)

        assert config.retry_on == (500, 429)

    def test_a_valid_configuration_still_constructs(self):
        config = RetryConfig(
            max_retries=5,
            backoff_factor=0,
            timeout=1.5,
            retry_on=[100, 599],
            max_backoff=2,
        )

        assert config.retry_on == (100, 599)
        assert config.max_backoff == 2


class TestRetryConfigRejectsValuesThatSlipPastComparisons:
    @pytest.mark.parametrize("bad", [True, False, 1.5, "3", None])
    def test_max_retries_must_be_an_integer_that_is_not_a_bool(self, bad):
        with pytest.raises(ValueError, match="max_retries"):
            RetryConfig(max_retries=bad)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "1"])
    def test_backoff_factor_must_be_a_finite_number(self, bad):
        with pytest.raises(ValueError, match="backoff_factor"):
            RetryConfig(backoff_factor=bad)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "1"])
    def test_timeout_must_be_a_finite_number(self, bad):
        with pytest.raises(ValueError, match="timeout"):
            RetryConfig(timeout=bad)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "1"])
    def test_max_backoff_must_be_a_finite_number(self, bad):
        with pytest.raises(ValueError, match="max_backoff"):
            RetryConfig(max_backoff=bad)

    @pytest.mark.parametrize("bad", [True, 99, 600, 0, -1, "500", 500.0, None])
    def test_retry_on_entries_must_be_http_statuses(self, bad):
        with pytest.raises(ValueError, match="retry_on"):
            RetryConfig(retry_on=[500, bad])
