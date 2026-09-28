"""Everything Google Play, in one module.

Five roles, the same five ``app_reviews.appstore`` has:

- ``auth``: RS256 JWT plus the OAuth token exchange for the Developer API
- ``web``: the public batchexecute provider (no credentials)
- ``developer_api``: the Google Play Developer API provider (credentials)
- ``reviews``: the reviews client, which picks a provider from the credentials
- ``search``: search and lookup by scraping the store pages

Plus ``replies``, for the Developer API's review replies; its client is exported
from ``app_reviews``.

The machinery all of this plugs into lives in ``app_reviews.core``.

The two clients are re-exported for symmetry, but import those from
``app_reviews``, which is the documented path. What this module is *for* is the
providers: reach in here to drive one directly, to pin a source rather than
letting credentials choose it, or to build a client of your own on
``app_reviews.core``. ``GoogleAuth`` comes with them, because the official
provider needs a token source and this is the one that performs the OAuth
exchange.
"""

from app_reviews.googleplay.auth import GoogleAuth
from app_reviews.googleplay.developer_api import GooglePlayOfficialProvider
from app_reviews.googleplay.reviews import GooglePlayReviews
from app_reviews.googleplay.search import GooglePlaySearch
from app_reviews.googleplay.web import GooglePlayScraperProvider

__all__ = [
    "GoogleAuth",
    "GooglePlayOfficialProvider",
    "GooglePlayReviews",
    "GooglePlayScraperProvider",
    "GooglePlaySearch",
]
