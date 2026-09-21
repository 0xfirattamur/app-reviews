"""The closed vocabularies: which store, which source, and the enums.

All the Literals and the sort enum live here rather than beside whichever
dataclass happened to use them first. ``ErrorKind`` and ``StopReason`` in
particular were defined in ``result.py``, so anything that only wanted to branch
on an error kind had to import the module that owns ``FetchResult``.
"""

from enum import StrEnum
from typing import Literal

Store = Literal["appstore", "googleplay"]

Source = Literal[
    "appstore_scraper",
    "appstore_official",
    "googleplay_scraper",
    "googleplay_official",
]

ErrorKind = Literal[
    "rate_limited",  # 429
    "auth",  # 401, 403
    "not_found",  # 404
    "request",  # permanent 4xx request/client error
    "server",  # 5xx
    "transport",  # connection failure or timeout
    "parse",  # malformed response body
]

StopReason = Literal[
    "exhausted",  # the provider ran out of pages
    "limit",  # the caller's limit was reached, more data exists
    "since",  # a page predated `since`, so paging stopped early
    "cycle",  # the source repeated a cursor, so the walk would not end
    "stalled",  # the source issued cursors but stopped returning reviews
    "max_pages",  # the walk hit its page ceiling, more data may exist
    "error",  # the walk failed
]

RETRYABLE_KINDS: frozenset[ErrorKind] = frozenset(
    {"rate_limited", "server", "transport"}
)
"""Which kinds are worth retrying.

Here rather than in ``core`` because a model reads it, and models must not import
the engine.
"""


class Sort(StrEnum):
    """Ordering for ``fetch``. Only ``NEWEST`` can bound the page walk."""

    NEWEST = "newest"
    OLDEST = "oldest"
    RATING = "rating"
