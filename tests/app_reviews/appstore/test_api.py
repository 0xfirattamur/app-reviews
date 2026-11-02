"""The URL check shared by every Connect call that follows a ``links.next``."""

import pytest

from app_reviews.appstore.api import CONNECT_HOST, refuse_foreign_url

_PATH = "/v1/apps/123/customerReviews"
_URL = f"https://{CONNECT_HOST}{_PATH}"


def _refusal(url: str) -> str | None:
    return refuse_foreign_url(url, host=CONNECT_HOST, exact_path=_PATH)


class TestRefuseForeignUrl:
    @pytest.mark.parametrize(
        "url",
        [
            _URL,
            _URL + "?cursor=2&limit=200",
            f"https://{CONNECT_HOST}:443{_PATH}",
            f"https://{CONNECT_HOST.upper()}{_PATH}",
        ],
    )
    def test_the_same_endpoint_is_allowed(self, url):
        assert _refusal(url) is None

    @pytest.mark.parametrize(
        ("url", "reason"),
        [
            (_URL + "?cursor=2\nforged record", "control characters"),
            (_URL + "?cursor=2\r", "control characters"),
            (_URL + "\t", "control characters"),
            (f"https://[::1{_PATH}", "cannot be parsed"),
            (f"http://{CONNECT_HOST}{_PATH}", "https"),
            (f"https://user@{CONNECT_HOST}{_PATH}", "user-info"),
            (f"https://user:pass@{CONNECT_HOST}{_PATH}", "user-info"),
            (f"https://evil.test{_PATH}", f"on {CONNECT_HOST}"),
            (f"https://{CONNECT_HOST}.evil.test{_PATH}", f"on {CONNECT_HOST}"),
            (f"https://{CONNECT_HOST}:99999{_PATH}", "invalid port"),
            (f"https://{CONNECT_HOST}:8443{_PATH}", "default port, got 8443"),
            (f"https://{CONNECT_HOST}/v1/users", f"exact path {_PATH}"),
            (f"https://{CONNECT_HOST}{_PATH}/extra", f"exact path {_PATH}"),
            (_URL + "#fragment", "fragments"),
        ],
    )
    def test_anything_else_is_refused_with_its_reason(self, url, reason):
        refusal = _refusal(url)

        assert refusal is not None
        assert reason in refusal
