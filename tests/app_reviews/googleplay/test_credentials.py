"""Tests for ServiceAccountCredentials validation."""

import pytest

from tests.app_reviews.factories import service_account_credentials


class TestServiceAccountCredentialsValidation:
    def test_valid_credentials(self):
        creds = service_account_credentials()
        assert creds.client_email == "test@test.iam.gserviceaccount.com"

    def test_empty_client_email_raises(self):
        with pytest.raises(ValueError, match="client_email"):
            service_account_credentials(client_email="")

    def test_empty_private_key_raises(self):
        with pytest.raises(ValueError, match="private_key_pem"):
            service_account_credentials(private_key_pem="")

    def test_non_pem_private_key_raises(self):
        with pytest.raises(ValueError, match="PEM"):
            service_account_credentials(private_key_pem="not-a-pem-key")

    def test_empty_token_uri_raises(self):
        with pytest.raises(ValueError, match="token_uri"):
            service_account_credentials(token_uri="")
