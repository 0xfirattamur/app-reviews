"""Everything the caller constructs and passes in: credentials and retry policy.

Inputs built before a fetch, as opposed to the results you get back.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from app_reviews.models._validate import (
    require_count,
    require_finite_non_negative,
    require_finite_positive,
    require_http_statuses,
)


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """HTTP retry and timeout settings."""

    max_retries: int = 3
    backoff_factor: float = 0.5
    timeout: float = 30.0
    retry_on: Collection[int] = field(default_factory=lambda: (500, 502, 503, 504, 429))

    max_backoff: float = 60.0
    """Ceiling on one wait, in seconds.

    ``backoff_factor * 2**attempt`` doubles without bound and ``max_retries`` has
    no upper limit, so an uncapped schedule let a single request sit for days on
    its last attempt. Also caps a ``Retry-After`` the server asks for.
    """

    def __post_init__(self) -> None:
        object.__setattr__(self, "retry_on", tuple(self.retry_on))
        require_count(self.max_retries, "max_retries")
        require_finite_non_negative(self.backoff_factor, "backoff_factor")
        require_finite_positive(self.timeout, "timeout")
        require_finite_positive(self.max_backoff, "max_backoff")
        require_http_statuses(self.retry_on, "retry_on")


@dataclass(frozen=True, slots=True, init=False)
class AppStoreAuth:
    """Apple App Store Connect API credentials.

    Give the ``.p8`` key either as a file (``key_path``) or as its PEM text
    (``private_key``), for keys held in a secret manager or an environment
    variable. Exactly one of the two.

    ``__init__`` is written out rather than generated so it can unbind the key
    before refusing a bad combination: the generated one binds ``private_key`` as
    a parameter, where an error reporter capturing frame locals would find it.
    """

    key_id: str
    issuer_id: str
    key_path: str | None
    private_key: str | None = field(repr=False)
    """The ``.p8`` contents. Kept out of ``repr``; see
    ``ConnectCredentials.private_key``."""

    def __init__(
        self,
        key_id: str,
        issuer_id: str,
        key_path: str | None = None,
        private_key: str | None = None,
    ) -> None:
        ambiguous = (key_path is None) == (private_key is None)
        object.__setattr__(self, "key_id", key_id)
        object.__setattr__(self, "issuer_id", issuer_id)
        object.__setattr__(self, "key_path", key_path)
        object.__setattr__(self, "private_key", private_key)
        del private_key
        if ambiguous:
            raise ValueError("Pass exactly one of key_path or private_key.")


@dataclass(frozen=True, slots=True, init=False)
class GooglePlayAuth:
    """Google Play Developer API credentials.

    Give the service-account key either as a file (``service_account_path``) or
    as its already-parsed JSON object (``service_account_info``). Exactly one of
    the two. ``__init__`` is written out for the reason ``AppStoreAuth`` gives.
    """

    service_account_path: str | None
    service_account_info: Mapping[str, Any] | None = field(repr=False, hash=False)
    """The parsed service-account JSON. Kept out of ``repr`` because it holds the
    private key, and out of the hash because a mapping has none."""

    def __init__(
        self,
        service_account_path: str | None = None,
        service_account_info: Mapping[str, Any] | None = None,
    ) -> None:
        ambiguous = (service_account_path is None) == (service_account_info is None)
        object.__setattr__(self, "service_account_path", service_account_path)
        object.__setattr__(self, "service_account_info", service_account_info)
        del service_account_info
        if ambiguous:
            raise ValueError(
                "Pass exactly one of service_account_path or service_account_info."
            )


@dataclass(frozen=True, slots=True)
class ConnectCredentials:
    """Validated credentials for App Store Connect API authentication."""

    key_id: str
    issuer_id: str
    private_key: str = field(repr=False)
    """Kept out of ``repr`` so the PEM cannot reach a log line, a traceback
    frame or an error reporter that captures locals."""

    def __post_init__(self) -> None:
        if not self.key_id:
            raise ValueError("key_id must not be empty.")
        if not self.issuer_id:
            raise ValueError("issuer_id must not be empty.")
        if not isinstance(self.private_key, str):
            raise TypeError("private_key must be a str.")
        if not self.private_key:
            raise ValueError("private_key must not be empty.")
        if "-----BEGIN" not in self.private_key:
            raise ValueError("private_key must be a PEM-encoded key.")
