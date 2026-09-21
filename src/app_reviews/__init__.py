"""app-reviews: Python package for App Store and Google Play reviews.

This module is the API. Every name below is importable straight from
``app_reviews``, and that is the supported way in:

    from app_reviews import AppStoreReviews, Country, Sort

Reviews come back as ``Review`` objects; ``FetchResult.to_dicts()`` gives you
JSON-serialisable plain dicts when you want to hand them to ``json`` or ``csv``.

Nothing else needs importing from a submodule path, which is deliberate: it is
what lets the package's internal shape change without breaking callers. Two
submodules are reasonable to reach into anyway, both documented:

- ``app_reviews.appstore`` / ``app_reviews.googleplay`` expose that store's
  provider classes, for driving a specific provider or building your own client.
  Single-app metadata lives on the search clients: ``AppStoreSearch().lookup(id)``.
- ``app_reviews.core`` is the store-agnostic engine. Internal, no promises.

The imports below are grouped by role; ``__all__`` stays alphabetical.
"""

import logging
from importlib.metadata import version

from app_reviews.appstore import AppStoreReviews, AppStoreSearch
from app_reviews.core.http import HttpClient
from app_reviews.errors import (
    AppReviewsError,
    AuthError,
    HttpError,
    NotFoundError,
    ParseError,
    RateLimitError,
    RequestError,
    ServerError,
    TransportError,
)
from app_reviews.googleplay import GooglePlayReviews, GooglePlaySearch
from app_reviews.models.config import AppStoreAuth, GooglePlayAuth, RetryConfig
from app_reviews.models.country import Country
from app_reviews.models.metadata import AppMetadata
from app_reviews.models.page import PageResult
from app_reviews.models.result import CountryOutcome, FetchError, FetchResult
from app_reviews.models.review import Review
from app_reviews.models.types import ErrorKind, Sort, Source, StopReason, Store

logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = version("app-reviews")

__all__ = [
    "AppMetadata",
    "AppReviewsError",
    "AppStoreAuth",
    "AppStoreReviews",
    "AppStoreSearch",
    "AuthError",
    "Country",
    "CountryOutcome",
    "ErrorKind",
    "FetchError",
    "FetchResult",
    "GooglePlayAuth",
    "GooglePlayReviews",
    "GooglePlaySearch",
    "HttpClient",
    "HttpError",
    "NotFoundError",
    "PageResult",
    "ParseError",
    "RateLimitError",
    "RequestError",
    "RetryConfig",
    "Review",
    "ServerError",
    "Sort",
    "Source",
    "StopReason",
    "Store",
    "TransportError",
    "__version__",
]
