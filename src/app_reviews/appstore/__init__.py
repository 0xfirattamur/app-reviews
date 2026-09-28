"""Everything App Store, in one module.

Five roles, the same five ``app_reviews.googleplay`` has:

- ``auth``: ES256 JWT signing for App Store Connect
- ``rss``: the public RSS feed provider (no credentials)
- ``connect``: the App Store Connect API provider (credentials)
- ``reviews``: the reviews client, which picks a provider from the credentials
- ``search``: search and lookup via the iTunes APIs

Plus what only App Store Connect offers: ``replies`` and ``versions``, on the
``api`` base they share. Their clients are exported from ``app_reviews``.

The machinery all of this plugs into lives in ``app_reviews.core``.

The two clients are re-exported for symmetry, but import those from
``app_reviews``, which is the documented path. What this module is *for* is the
providers: reach in here to drive one directly, to pin a source rather than
letting credentials choose it, or to build a client of your own on
``app_reviews.core``. ``ConnectAuth`` comes with them, because the official
provider needs a token source and this is the one that signs Connect JWTs.
"""

from app_reviews.appstore.auth import ConnectAuth
from app_reviews.appstore.connect import AppStoreOfficialProvider
from app_reviews.appstore.reviews import AppStoreReviews
from app_reviews.appstore.rss import AppStoreScraperProvider
from app_reviews.appstore.search import AppStoreSearch

__all__ = [
    "AppStoreOfficialProvider",
    "AppStoreReviews",
    "AppStoreScraperProvider",
    "AppStoreSearch",
    "ConnectAuth",
]
