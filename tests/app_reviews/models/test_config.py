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
