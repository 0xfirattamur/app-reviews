"""Everything the caller constructs and passes in.

Credentials and retry policy sat in two files of 57 and 21 lines. They are the
same kind of thing (inputs you build before a fetch, as opposed to the results
you get back), so they share a module.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

GOOGLE_TOKEN_HOSTS = frozenset({"oauth2.googleapis.com", "sts.googleapis.com"})
"""The only hosts a signed assertion may be POSTed to.

An exact set, not a ``.googleapis.com`` suffix: that domain is a shared
multi-tenant namespace, so a suffix match accepts ``storage.googleapis.com`` and
any other Google-hosted endpoint as a destination for the caller's credential.
``sts`` is here for workload identity federation.
"""


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
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.backoff_factor < 0:
            raise ValueError("backoff_factor must be >= 0")
        if self.timeout <= 0:
            raise ValueError("timeout must be > 0")
        if self.max_backoff <= 0:
            raise ValueError("max_backoff must be > 0")


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
        if not self.private_key:
            raise ValueError("private_key must not be empty.")
        if "-----BEGIN" not in self.private_key:
            raise ValueError("private_key must be a PEM-encoded key.")


@dataclass(frozen=True, slots=True)
class ServiceAccountCredentials:
    """Validated credentials for Google Play service account authentication."""

    client_email: str
    private_key_pem: str = field(repr=False)
    """Kept out of ``repr``; see ``ConnectCredentials.private_key``."""

    token_uri: str

    def __post_init__(self) -> None:
        if not self.client_email:
            raise ValueError("client_email must not be empty.")
        if not self.private_key_pem:
            raise ValueError("private_key_pem must not be empty.")
        if "-----BEGIN" not in self.private_key_pem:
            raise ValueError("private_key_pem must be a PEM-encoded key.")
        if not self.token_uri:
            raise ValueError("token_uri must not be empty.")
        self._check_token_uri()

    def _check_token_uri(self) -> None:
        """The token endpoint is checked, not trusted.

        It arrives in the service-account file rather than from this package, and
        it is where a JWT signed with ``private_key_pem`` gets POSTed, so it has
        to be HTTPS, and it has to be Google.
        """
        parsed = urlsplit(self.token_uri)
        if parsed.scheme != "https":
            raise ValueError(
                f"token_uri must be https, got a non-HTTPS URL "
                f"({self.token_uri!r}); the signed assertion would travel in "
                f"plaintext."
            )
        host = parsed.hostname or ""
        if host not in GOOGLE_TOKEN_HOSTS:
            raise ValueError(
                f"token_uri host {host!r} is not a Google token endpoint; the "
                f"assertion is a bearer credential and must not be sent "
                f"elsewhere. Expected one of {sorted(GOOGLE_TOKEN_HOSTS)}."
            )
