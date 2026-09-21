"""What the ladder branches on: per-source facts, and the decisions a walk makes.

``reviews.py`` owns the loops. Everything they *decide* is here, once, so the sync
and async bodies (which cannot share code, an async generator being a different
animal) cannot drift apart.

The two per-source facts live here too. They are dispatch, not a published
capability table: one gates the country fan-out and the other gates the ``since``
early stop, and keeping the pair adjacent means a fifth source is one edit rather
than two. "How do these sources differ" is a question you have while *writing*
code, so it is answered in ``docs/reference/capabilities.md``.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime

from app_reviews.models.page import PageResult
from app_reviews.models.result import CountryOutcome, FetchError, to_aware_datetime
from app_reviews.models.review import Review
from app_reviews.models.types import Source, StopReason

_LOG = logging.getLogger(__name__)


_PER_COUNTRY: frozenset[str] = frozenset({"appstore_scraper"})
"""Sources with a country dimension at all.

Only the App Store RSS feed has one: its URL is per storefront. Connect and both
Play endpoints are global (one request covers every territory), so fanning out
over countries there would repeat the same request N times for the same data.
"""

_NEWEST_FIRST: frozenset[str] = frozenset(
    {
        "appstore_scraper",  # RSS sortBy=mostRecent
        "appstore_official",  # Connect sort=-createdDate
        "googleplay_scraper",  # batchexecute SORT_NEWEST
    }
)
"""Sources whose pages are guaranteed newest-first.

``googleplay_official`` is absent on purpose: the Developer API documents no
ordering, and stopping a walk early on an unverified assumption silently loses
reviews.
"""


def is_per_country(source: Source) -> bool:
    """Whether a country fan-out means anything for this source."""
    return source in _PER_COUNTRY


def orders_newest_first(source: Source) -> bool:
    """Whether this source's pages can be trusted to arrive newest-first.

    Gates the ``since`` early stop: without a guaranteed order, a later page
    could still hold reviews inside the window.
    """
    return source in _NEWEST_FIRST


def with_stop_reason(page: PageResult, reason: StopReason | None) -> PageResult:
    """Return the page, stamped with a stop reason if the walk ended on it."""
    if reason is None:
        return page
    return PageResult(
        reviews=page.reviews,
        next_cursor=page.next_cursor,
        error=page.error,
        stopped_because=reason,
        skipped_reviews=page.skipped_reviews,
    )


class CountryCollector:
    """Accumulates one country's pages into a ``CountryOutcome``.

    The clock starts at construction, so build this immediately before the loop.
    """

    def __init__(self, country: str | None) -> None:
        self._country = country
        self._started = time.monotonic()
        self.reviews: list[Review] = []
        self._pages = 0
        self._reason: StopReason = "exhausted"
        self._error: FetchError | None = None
        self._skipped_reviews = 0

    def add(self, page: PageResult) -> None:
        """Fold one page in. Only a page carrying a stop reason sets one."""
        self._pages += 1
        self.reviews.extend(page.reviews)
        self._skipped_reviews += page.skipped_reviews
        if page.stopped_because is not None:
            self._reason = page.stopped_because
            self._error = page.error

    def outcome(self) -> CountryOutcome:
        """Summarise the walk. ``exhausted`` unless a page said otherwise."""
        return CountryOutcome(
            country=self._country,
            pages=self._pages,
            reviews_fetched=len(self.reviews),
            stopped_because=self._reason,
            error=self._error,
            elapsed=time.monotonic() - self._started,
            skipped_reviews=self._skipped_reviews,
        )


DEFAULT_MAX_PAGES = 10_000
"""Pages one country walk will request before giving up.

The walk's own floor, not a caller's preference. Every other stop condition
depends on the source behaving: ``exhausted`` needs it to stop issuing cursors,
``cycle`` needs it to repeat one. A source that does neither would spin forever,
so the walk counts its own pages. Set high enough to be unreachable in practice
(at 200 reviews a page, 2M reviews per storefront) and low enough that the
retained cursor set stays small.
"""

DEFAULT_MAX_EMPTY_PAGES = 3
"""Consecutive review-less pages before a walk is declared stalled.

``limit`` and ``since`` are both driven by reviews actually seen, so a source
returning empty pages with fresh cursors escapes them both: a caller who
passed ``limit=5`` would still walk forever. A *streak* rather than a single
page, because one empty page followed by more data is normal (a Play page whose
rows all failed to parse, a filtered Connect page).
"""


class StopPolicy:
    """Decides when a walk ends. One instance per country walk, since it keeps count."""

    def __init__(
        self,
        source: Source,
        since: date | datetime | None,
        limit: int | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_empty_pages: int = DEFAULT_MAX_EMPTY_PAGES,
    ) -> None:
        self._since = to_aware_datetime(since) if since is not None else None
        self._trust_order = orders_newest_first(source)
        self._limit = limit
        self._max_pages = max_pages
        self._max_empty_pages = max_empty_pages
        self._seen = 0
        self._pages = 0
        self._empty_streak = 0
        self._cursors: set[str] = set()

    def evaluate(self, page: PageResult) -> tuple[StopReason | None, str | None]:
        """Decide whether the walk ends on this page.

        Returns ``(stop_reason, next_cursor)``. A non-None reason ends the walk.

        Precedence is deliberate, and the rule is one-way: "there is no more
        data" is never reported as "we stopped asking". So ``exhausted`` outranks
        ``limit`` *and* ``since``: a final page that also predates ``since`` had
        nothing after it either way, and saying ``since`` would imply data was
        left behind.

        ``stalled`` and ``max_pages`` rank last for the same reason. They mean
        "the walk gave up on a source that would not end", so any reason the
        source or the caller supplied is the truer answer.
        """
        if page.error is not None:
            return "error", None
        self._seen += len(page.reviews)
        self._pages += 1
        self._empty_streak = 0 if page.reviews else self._empty_streak + 1
        if not page.next_cursor:
            return "exhausted", None
        if self._predates_since(page):
            return "since", None
        if self._limit is not None and self._seen >= self._limit:
            return "limit", None
        if page.next_cursor in self._cursors:
            _LOG.warning(
                "Ending the walk: the source repeated cursor %r, so following it "
                "again would not advance",
                page.next_cursor,
            )
            return "cycle", None
        if self._empty_streak >= self._max_empty_pages:
            _LOG.warning(
                "Ending the walk: the source returned %d consecutive pages with no "
                "reviews while still issuing cursors, so it is not advancing",
                self._empty_streak,
            )
            return "stalled", None
        if self._pages >= self._max_pages:
            _LOG.warning(
                "Ending the walk: reached the %d-page ceiling with a cursor still "
                "outstanding. More data may exist; resume from the last cursor",
                self._max_pages,
            )
            return "max_pages", None
        self._cursors.add(page.next_cursor)
        return None, page.next_cursor

    def _predates_since(self, page: PageResult) -> bool:
        """True if this page's oldest review is older than ``since``.

        Requires a guaranteed newest-first ordering: without it, a later page
        could still hold reviews inside the window.
        """
        if self._since is None or not self._trust_order or not page.reviews:
            return False
        return page.reviews[-1].dated_at < self._since
