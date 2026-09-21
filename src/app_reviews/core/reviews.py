"""Base reviews client: the page/cursor ladder and the fetch pipeline."""

from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Collection, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any

from app_reviews.core.classify import fetch_error_from_exception
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient
from app_reviews.core.paging import (
    DEFAULT_MAX_PAGES,
    CountryCollector,
    StopPolicy,
    is_per_country,
    orders_newest_first,
    prefer_stop_reason,
    with_stop_reason,
)
from app_reviews.core.provider import ReviewProvider
from app_reviews.core.validation import require_non_negative, require_positive
from app_reviews.errors import AppReviewsError, AuthError
from app_reviews.models.config import RetryConfig
from app_reviews.models.country import Country, normalise_country
from app_reviews.models.page import PageResult
from app_reviews.models.result import (
    CountryOutcome,
    FetchResult,
    to_aware_datetime,
)
from app_reviews.models.review import Review
from app_reviews.models.types import Sort, Source

_LOG = logging.getLogger(__name__)


class BaseReviews(PooledClient, abc.ABC):
    """Base reviews client.

    Four rungs, each built on the one below and mirrored sync/async:

    - ``fetch_page``: one request. Cursor in, cursor out. Persist the cursor
      to resume the walk later, in another process if you like.
    - ``iter_pages``: one country, paginated. Owns the ``since`` early stop.
    - ``iter_reviews``: every country, streamed one review at a time.
    - ``fetch``: all countries, concurrent, filtered and sorted.

    **Start at ``fetch``.** It is the one that answers "give me this app's
    reviews"; the rungs below it exist for callers who need to bound memory, own
    the pagination, or checkpoint a walk.

    The rungs deliberately disagree about failure, because what a caller *can* do
    about one differs by rung. Reading down the ladder:

    ===============  ==========================================================
    Rung             What a failed page does
    ===============  ==========================================================
    ``fetch_page``   Returns the page with ``.error`` set. Nothing is retried
                     past the HTTP client's own policy; you decide.
    ``iter_pages``   Yields that page, with ``.error`` and ``stopped_because ==
                     "error"``, then stops. Earlier pages already reached you.
    ``iter_reviews`` Logs and moves to the next country. A stream of reviews has
                     nowhere to put an error object, and one dead storefront
                     should not end a 155-storefront walk.
    ``fetch``        Records it on that country's ``CountryOutcome``, which
                     ``FetchResult.errors`` reads back. Reviews from the
                     countries that worked are still returned.
    ===============  ==========================================================

    ``AuthError`` is the carve-out at every rung: it is raised, not reported. An
    unusable credential is configuration, identical for every country and page,
    so reporting it N times would bury the one fact that matters.
    """

    DEFAULT_COUNTRY = "us"
    """Walked when a per-country source is given no countries."""

    MAX_PAGES = DEFAULT_MAX_PAGES
    """Default pages one country walk requests before reporting ``max_pages``.

    This is the safety ceiling when a call omits ``max_pages``. Every paginated
    public rung accepts a per-call override; use the final page's cursor to
    resume a bounded walk.
    """

    DEFAULT_CONCURRENCY = 8
    """Default maximum number of simultaneous storefront walks."""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        retry: RetryConfig | None = None,
        http: HttpClient | None = None,
    ) -> None:
        super().__init__(proxy=proxy, retry=retry, http=http)
        self._cached_provider: ReviewProvider | None = None

    @property
    def _provider_kwargs(self) -> dict[str, Any]:
        return {"http": self._http}

    @abc.abstractmethod
    def _build_provider(self) -> ReviewProvider: ...

    async def _abuild_provider(self) -> ReviewProvider:
        """Build a provider from an async context.

        Defaults to the sync construction. Overridden where obtaining
        credentials performs I/O, so the async ladder never blocks the event
        loop on a token exchange.
        """
        return self._build_provider()

    def _ensure_provider(self) -> ReviewProvider:
        """This client's provider, built at most once.

        Construction is credential-bearing and expensive: the App Store path
        reads a .p8 off disk and signs an ES256 JWT, the Play path performs an
        OAuth token exchange. Building per call made ``source`` perform network
        I/O and made the resume-by-cursor loop re-authenticate on every page.
        """
        if self._cached_provider is None:
            self._cached_provider = self._build_provider()
        return self._cached_provider

    async def _aensure_provider(self) -> ReviewProvider:
        """``_ensure_provider`` for async callers, reusing a sync-built one if present.

        Unlocked on purpose. Two concurrent first calls can both build, but
        construction is idempotent, so the cost is a redundant build rather
        than a wrong provider, and cheaper than serialising every async entry
        point behind a lock for a race that costs one token exchange.
        """
        if self._cached_provider is None:
            self._cached_provider = await self._abuild_provider()
        return self._cached_provider

    @property
    def source(self) -> Source:
        """Which source this client will read from, given its credentials."""
        return self._ensure_provider().source

    def resolve_countries(
        self, countries: Collection[Country | str] | None = None
    ) -> list[str]:
        """Resolve requested countries to what will actually be fetched.

        Returns ``[""]`` for global sources, where the country dimension does
        not exist and a fan-out would be decorative: one request covers every
        territory. Keyed off ``core.paging.is_per_country``, which is the
        only home for that fact.
        """
        self._validate_countries_argument(countries)
        selection = self._normalised_selection(countries)
        if selection == []:
            return []
        return self._country_list(self._ensure_provider(), selection)

    # ---- rung 1: one request ------------------------------------------------

    def fetch_page(
        self,
        app_id: str,
        *,
        country: Country | str | None = None,
        cursor: str | None = None,
    ) -> PageResult:
        """Fetch exactly one page. No pagination, no filtering, no stop conditions.

        ``cursor`` is opaque and provider-specific. Persist it verbatim to
        resume later. A returned ``next_cursor`` of None means no more pages.
        ``stopped_because`` is always None here, because a single request has nothing
        to stop.
        """
        self._validate_country_argument(country)
        provider = self._ensure_provider()
        return provider.fetch_page(app_id, self._country_arg(provider, country), cursor)

    async def afetch_page(
        self,
        app_id: str,
        *,
        country: Country | str | None = None,
        cursor: str | None = None,
    ) -> PageResult:
        """Async equivalent of ``fetch_page``."""
        self._validate_country_argument(country)
        provider = await self._aensure_provider()
        return await provider.afetch_page(
            app_id, self._country_arg(provider, country), cursor
        )

    # ---- rung 2: one country, paginated ------------------------------------

    def iter_pages(
        self,
        app_id: str,
        *,
        country: Country | str | None = None,
        cursor: str | None = None,
        since: date | datetime | None = None,
        limit: int | None = None,
        max_pages: int | None = None,
    ) -> Iterator[PageResult]:
        """Walk one country's pages, yielding each.

        The final page yielded carries ``stopped_because``. On a failed page
        this yields that page with ``.error`` set and then stops, rather than
        raising or silently discarding the error. The one exception is
        ``AuthError``: an unusable credential is configuration, identical for
        every country and page, so it is raised rather than reported N times.

        ``since`` stops the walk once a page's oldest review predates it, so
        the requests are never made. Applied only when
        ``core.paging.orders_newest_first(source)`` is True; otherwise a
        later page could still hold newer reviews.

        ``limit`` stops the walk once that many reviews have been yielded.
        ``stopped_because == "limit"`` means more data exists;
        ``"exhausted"`` means it does not.

        ``max_pages`` is the maximum number of requests in this country walk.
        Zero performs no I/O; omission uses the client's safety ceiling.
        """
        self._validate_country_argument(country)
        require_non_negative(limit, "limit")
        page_budget = self._page_budget(max_pages)
        if limit == 0 or page_budget == 0:
            return
        provider = self._ensure_provider()
        yield from self._walk(
            provider,
            app_id,
            self._country_arg(provider, country),
            cursor,
            since,
            limit,
            page_budget,
        )

    async def aiter_pages(
        self,
        app_id: str,
        *,
        country: Country | str | None = None,
        cursor: str | None = None,
        since: date | datetime | None = None,
        limit: int | None = None,
        max_pages: int | None = None,
    ) -> AsyncIterator[PageResult]:
        """Async equivalent of ``iter_pages``, with identical semantics."""
        self._validate_country_argument(country)
        require_non_negative(limit, "limit")
        page_budget = self._page_budget(max_pages)
        if limit == 0 or page_budget == 0:
            return
        provider = await self._aensure_provider()
        async for page in self._awalk(
            provider,
            app_id,
            self._country_arg(provider, country),
            cursor,
            since,
            limit,
            page_budget,
        ):
            yield page

    def _walk(
        self,
        provider: ReviewProvider,
        app_id: str,
        country: str,
        cursor: str | None,
        since: date | datetime | None,
        limit: int | None,
        max_pages: int,
    ) -> Iterator[PageResult]:
        """The page walk. Takes an already-built provider so rung 3 can reuse it.

        ``iter_pages`` builds a provider and delegates here; ``fetch`` already
        holds one and delegates here too. Rebuilding it per country would
        re-sign credentials on every call.
        """
        policy = StopPolicy(
            provider.source,
            since,
            limit,
            max_pages=max_pages,
            initial_cursor=cursor,
        )

        while True:
            page = self._page(provider, app_id, country, cursor)
            reason, next_cursor = policy.evaluate(page)
            yield with_stop_reason(page, reason)
            if reason is not None:
                return
            cursor = next_cursor

    async def _awalk(
        self,
        provider: ReviewProvider,
        app_id: str,
        country: str,
        cursor: str | None,
        since: date | datetime | None,
        limit: int | None,
        max_pages: int,
    ) -> AsyncIterator[PageResult]:
        """Async equivalent of ``_walk``.

        The loop body is written twice because an async generator cannot share
        one with a sync generator. The stop *policy* is not duplicated; both
        delegate to ``StopPolicy``.
        """
        policy = StopPolicy(
            provider.source,
            since,
            limit,
            max_pages=max_pages,
            initial_cursor=cursor,
        )

        while True:
            page = await self._apage(provider, app_id, country, cursor)
            reason, next_cursor = policy.evaluate(page)
            yield with_stop_reason(page, reason)
            if reason is not None:
                return
            cursor = next_cursor

    # ---- rung 2.5: all countries, streamed one review at a time ------------

    def iter_reviews(
        self,
        app_id: str,
        *,
        countries: Collection[Country | str] | None = None,
        since: date | datetime | None = None,
        limit: int | None = None,
        max_pages: int | None = None,
    ) -> Iterator[Review]:
        """Stream reviews across countries, one at a time, buffering nothing.

        Sits between ``iter_pages`` and ``fetch``. ``iter_pages`` streams but
        yields pages, so callers wrote the same nested loop every time;
        ``fetch`` yields reviews but must hold every review of every country in
        memory before it can filter, sort and limit across the whole set, and with
        ``Country.ALL`` that is 155 storefronts at once. This holds one page.

        The trade is deliberate: no cross-country sorting, because that is the
        one thing here that needs the full set in hand. ``ratings`` and ``until``
        are simply not arguments of this rung: both are per-review tests you can
        apply to the stream yourself. Reviews arrive in fetch order, country by
        country. Use ``fetch`` when you want the N best; use this when you want to
        process a corpus that does not fit in memory, or to stop early.

        ``limit`` here means "yield at most this many", counted across
        countries, not ``fetch``'s "the N best under ``sort``".

        ``max_pages`` bounds each country walk, including filtered streams.

        Countries are walked in sequence, not concurrently: a concurrent fan-out
        would have to buffer to put results back in order, which is the cost
        this rung exists to avoid.

        A country whose walk fails is logged and skipped, because a generator has
        nowhere to hand a ``FetchError`` back. Use ``fetch`` or ``iter_pages``
        when you need the failure as data.
        """
        self._validate_countries_argument(countries)
        require_non_negative(limit, "limit")
        page_budget = self._page_budget(max_pages)
        selection = self._normalised_selection(countries)
        if limit == 0 or page_budget == 0 or selection == []:
            return
        provider = self._ensure_provider()
        boundary = self._since_boundary(since)
        yielded = 0

        for country in self._country_list(provider, selection):
            if limit is not None and yielded >= limit:
                return
            remaining = self._stream_walk_limit(provider, since, limit, yielded)
            for page in self._walk(
                provider, app_id, country, None, since, remaining, page_budget
            ):
                self._log_stream_error(page, country)
                for review in page.reviews:
                    if boundary is not None and review.dated_at < boundary:
                        continue
                    if limit is not None and yielded >= limit:
                        return
                    yield review
                    yielded += 1

    async def aiter_reviews(
        self,
        app_id: str,
        *,
        countries: Collection[Country | str] | None = None,
        since: date | datetime | None = None,
        limit: int | None = None,
        max_pages: int | None = None,
    ) -> AsyncIterator[Review]:
        """Async equivalent of ``iter_reviews``, with identical semantics."""
        self._validate_countries_argument(countries)
        require_non_negative(limit, "limit")
        page_budget = self._page_budget(max_pages)
        selection = self._normalised_selection(countries)
        if limit == 0 or page_budget == 0 or selection == []:
            return
        provider = await self._aensure_provider()
        boundary = self._since_boundary(since)
        yielded = 0

        for country in self._country_list(provider, selection):
            if limit is not None and yielded >= limit:
                return
            remaining = self._stream_walk_limit(provider, since, limit, yielded)
            async for page in self._awalk(
                provider, app_id, country, None, since, remaining, page_budget
            ):
                self._log_stream_error(page, country)
                for review in page.reviews:
                    if boundary is not None and review.dated_at < boundary:
                        continue
                    if limit is not None and yielded >= limit:
                        return
                    yield review
                    yielded += 1

    def _page(
        self,
        provider: ReviewProvider,
        app_id: str,
        country: str,
        cursor: str | None,
    ) -> PageResult:
        """One page, with a raised package error turned into page data.

        Providers report HTTP failures as ``PageResult.error``, but obtaining a
        token is I/O of its own and can fail by raising. Left alone, that escaped
        ``iter_pages``, which promises it never raises, and cost ``fetch`` every
        country that had already walked cleanly.

        ``AuthError`` is deliberately not converted. It means the credential
        itself is unusable, which every country and every page would repeat, so a
        ``FetchResult`` of N identical auth errors and no reviews is worse than one
        exception the caller can act on. Everything else concerns a single
        exchange and belongs on the page.

        A stdlib exception is a bug in here rather than a failure out there, and
        propagates untouched.
        """
        try:
            return provider.fetch_page(app_id, country, cursor)
        except AuthError:
            raise
        except AppReviewsError as exc:
            return PageResult(
                error=fetch_error_from_exception(country=country or None, exc=exc)
            )

    async def _apage(
        self,
        provider: ReviewProvider,
        app_id: str,
        country: str,
        cursor: str | None,
    ) -> PageResult:
        """Async twin of ``_page``."""
        try:
            return await provider.afetch_page(app_id, country, cursor)
        except AuthError:
            raise
        except AppReviewsError as exc:
            return PageResult(
                error=fetch_error_from_exception(country=country or None, exc=exc)
            )

    def _stream_walk_limit(
        self,
        provider: ReviewProvider,
        since: date | datetime | None,
        limit: int | None,
        yielded: int,
    ) -> int | None:
        """How many reviews the walk may stop at, for one country of a stream.

        ``limit`` counts reviews *yielded*; ``StopPolicy`` counts reviews
        *fetched*. The two only agree when nothing between them drops a review.
        A ``since`` filter does exactly that, and on a source without a
        newest-first guarantee there is no early stop to make the boundary page
        the last one, so the walk would stop on fetched count with matching
        reviews still unfetched. Left unbounded there; the stream's own counter
        ends it.
        """
        if limit is None:
            return None
        if since is not None and not orders_newest_first(provider.source):
            return None
        return limit - yielded

    def _since_boundary(self, since: date | datetime | None) -> datetime | None:
        """``since`` as a comparable instant, for filtering a stream.

        A streaming walk needs ``since`` twice. ``StopPolicy`` uses it to stop
        requesting pages, and it stops *on* the page whose oldest review predates
        the boundary: a page that still carries the newer reviews above it
        alongside the older ones below. ``fetch`` drops those through
        ``FetchResult.filter`` afterwards; a stream has nothing downstream to do
        it, so it filters as it yields.
        """
        return to_aware_datetime(since) if since is not None else None

    def _log_stream_error(self, page: PageResult, country: str) -> None:
        """Report a failed page on a streaming walk, which cannot return errors."""
        if page.error is not None:
            _LOG.warning(
                "iter_reviews: %s walk for %r failed: %s",
                page.error.kind,
                country or "global",
                page.error.message,
            )

    # ---- rung 3: all countries, concurrent, filtered and sorted -----------

    def fetch(
        self,
        app_id: str,
        *,
        countries: Collection[Country | str] | None = None,
        since: date | datetime | None = None,
        until: date | datetime | None = None,
        ratings: list[int] | None = None,
        sort: Sort = Sort.NEWEST,
        limit: int | None = None,
        concurrency: int | None = None,
        max_pages: int | None = None,
    ) -> FetchResult:
        """Fetch reviews across countries, then filter and sort.

        ``limit`` means "the N best under ``sort``". With ``Sort.NEWEST`` on a
        newest-first source, the walk stops once N qualifying reviews have been
        collected, including with ``ratings``/``until`` filters. Other sorts and
        sources without that ordering guarantee remain exhaustive because later
        pages can still displace an earlier result.

        ``concurrency`` bounds the cross-country fan-out. Pass 1 to make it
        sequential, for example when you are rate-limiting a source yourself.
        Omission caps the fan-out at eight. ``max_pages`` bounds each country
        walk so filtered requests have an explicit request budget.
        """
        self._validate_countries_argument(countries)
        require_non_negative(limit, "limit")
        require_positive(concurrency, "concurrency")
        page_budget = self._page_budget(max_pages)
        selection = self._normalised_selection(countries)
        if limit == 0 or page_budget == 0 or selection == []:
            return FetchResult()
        provider = self._ensure_provider()
        resolved = self._country_list(provider, selection)
        walk_limit = self._walk_limit(
            provider.source, sort, limit, ratings=ratings, until=until
        )
        qualifying = self._qualifying_filter(
            ratings=ratings,
            since=since,
            until=until,
        )
        qualifying_limit = self._qualifying_limit(
            provider.source, sort, limit, ratings=ratings, until=until
        )
        workers = self._workers(resolved, concurrency)

        collected: list[tuple[list[Review], CountryOutcome]]
        if workers == 1:
            collected = [
                self._walk_country(
                    provider,
                    app_id,
                    c,
                    walk_limit,
                    since,
                    page_budget,
                    qualifying,
                    qualifying_limit,
                )
                for c in resolved
            ]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(
                        self._walk_country,
                        provider,
                        app_id,
                        c,
                        walk_limit,
                        since,
                        page_budget,
                        qualifying,
                        qualifying_limit,
                    )
                    for c in resolved
                ]
                collected = [f.result() for f in futures]

        return self._assemble(collected, sort, limit)

    async def afetch(
        self,
        app_id: str,
        *,
        countries: Collection[Country | str] | None = None,
        since: date | datetime | None = None,
        until: date | datetime | None = None,
        ratings: list[int] | None = None,
        sort: Sort = Sort.NEWEST,
        limit: int | None = None,
        concurrency: int | None = None,
        max_pages: int | None = None,
    ) -> FetchResult:
        """Async equivalent of ``fetch``. Uses ``asyncio.gather``, not threads."""
        self._validate_countries_argument(countries)
        require_non_negative(limit, "limit")
        require_positive(concurrency, "concurrency")
        page_budget = self._page_budget(max_pages)
        selection = self._normalised_selection(countries)
        if limit == 0 or page_budget == 0 or selection == []:
            return FetchResult()
        provider = await self._aensure_provider()
        resolved = self._country_list(provider, selection)
        walk_limit = self._walk_limit(
            provider.source, sort, limit, ratings=ratings, until=until
        )
        qualifying = self._qualifying_filter(
            ratings=ratings,
            since=since,
            until=until,
        )
        qualifying_limit = self._qualifying_limit(
            provider.source, sort, limit, ratings=ratings, until=until
        )
        semaphore = asyncio.Semaphore(self._workers(resolved, concurrency))

        async def guarded(country: str) -> tuple[list[Review], CountryOutcome]:
            async with semaphore:
                return await self._awalk_country(
                    provider,
                    app_id,
                    country,
                    walk_limit,
                    since,
                    page_budget,
                    qualifying,
                    qualifying_limit,
                )

        # return_exceptions so one country's failure does not cancel the others
        # mid-flight, orphaning their requests on the shared pool. The threaded
        # sync twin waits for every worker; this makes the async path match.
        settled = await asyncio.gather(
            *(guarded(c) for c in resolved), return_exceptions=True
        )
        for outcome in settled:
            if isinstance(outcome, BaseException):
                raise outcome
        collected = [o for o in settled if not isinstance(o, BaseException)]
        return self._assemble(collected, sort, limit)

    def _walk_limit(
        self,
        source: Source,
        sort: Sort,
        limit: int | None,
        *,
        ratings: list[int] | None,
        until: date | datetime | None,
    ) -> int | None:
        """How many reviews the page walk may stop at, if any.

        Bounding the walk is only sound when the fetch order matches the
        requested order (newest-first source, newest-first sort), and when
        nothing downstream can drop reviews before the limit is applied.

        ``sort`` is coerced because ``Sort`` is a ``StrEnum`` and the equivalent
        plain string is accepted everywhere else: compared with ``is``, ``"newest"``
        took the slow path and then died reading ``.value`` off a ``str``.

        With ``ratings``/``until``, a raw-review limit would be unsound because
        some fetched rows are later discarded. ``_qualifying_filter`` handles
        that case by counting matches instead; this method therefore leaves the
        raw walk unbounded by review count.
        """
        if limit is None:
            return None
        if ratings is not None or until is not None:
            return None
        order = Sort(sort)
        if order is not Sort.NEWEST:
            _LOG.info(
                "sort=%s with limit=%d requires exhausting pagination; "
                "only sort=newest can stop the walk early",
                order.value,
                limit,
            )
            return None
        if not orders_newest_first(source):
            _LOG.info(
                "%s does not guarantee newest-first ordering, so limit=%d "
                "cannot stop the walk early",
                source,
                limit,
            )
            return None
        return limit

    def _qualifying_filter(
        self,
        *,
        ratings: list[int] | None,
        since: date | datetime | None,
        until: date | datetime | None,
    ) -> Callable[[Review], bool] | None:
        """Build the predicate applied before a page enters retained storage."""
        if ratings is None and since is None and until is None:
            return None

        rating_set = set(ratings) if ratings is not None else None
        since_boundary = to_aware_datetime(since) if since is not None else None
        until_boundary = (
            to_aware_datetime(until, end_of_day=True) if until is not None else None
        )

        def qualifies(review: Review) -> bool:
            return (
                (rating_set is None or review.rating in rating_set)
                and (since_boundary is None or review.dated_at >= since_boundary)
                and (until_boundary is None or review.dated_at <= until_boundary)
            )

        return qualifies

    def _qualifying_limit(
        self,
        source: Source,
        sort: Sort,
        limit: int | None,
        *,
        ratings: list[int] | None,
        until: date | datetime | None,
    ) -> int | None:
        """Limit that may stop a walk by counting filtered matches."""
        if limit is None or (ratings is None and until is None):
            return None
        if Sort(sort) is not Sort.NEWEST or not orders_newest_first(source):
            return None
        return limit

    def _workers(self, resolved: list[str], concurrency: int | None) -> int:
        """Resolve the fan-out width, conservatively bounded by default."""
        require_positive(concurrency, "concurrency")
        if concurrency is not None:
            return max(1, min(concurrency, len(resolved)))
        return min(self.DEFAULT_CONCURRENCY, len(resolved))

    def _walk_country(
        self,
        provider: ReviewProvider,
        app_id: str,
        country: str,
        walk_limit: int | None,
        since: date | datetime | None,
        max_pages: int,
        qualifying: Callable[[Review], bool] | None,
        qualifying_limit: int | None,
    ) -> tuple[list[Review], CountryOutcome]:
        """Drive rung 2 for one country and summarise what it did.

        Consumes ``_walk`` rather than re-implementing the page loop, so the
        stop conditions exist in exactly one place, and hands each page to a
        ``CountryCollector`` so the accounting does too.
        """
        collector = CountryCollector(self._outcome_country(provider, country))
        qualified = 0
        for page in self._walk(
            provider, app_id, country, None, since, walk_limit, max_pages
        ):
            wire_rows = len(page.reviews)
            page, qualified = self._with_qualifying_stop(
                page, qualifying, qualified, qualifying_limit
            )
            collector.add(page, reviews_fetched=wire_rows)
            if page.stopped_because is not None:
                break
        return collector.reviews, collector.outcome()

    async def _awalk_country(
        self,
        provider: ReviewProvider,
        app_id: str,
        country: str,
        walk_limit: int | None,
        since: date | datetime | None,
        max_pages: int,
        qualifying: Callable[[Review], bool] | None,
        qualifying_limit: int | None,
    ) -> tuple[list[Review], CountryOutcome]:
        """Async equivalent of ``_walk_country``."""
        collector = CountryCollector(self._outcome_country(provider, country))
        qualified = 0
        async for page in self._awalk(
            provider, app_id, country, None, since, walk_limit, max_pages
        ):
            wire_rows = len(page.reviews)
            page, qualified = self._with_qualifying_stop(
                page, qualifying, qualified, qualifying_limit
            )
            collector.add(page, reviews_fetched=wire_rows)
            if page.stopped_because is not None:
                break
        return collector.reviews, collector.outcome()

    def _with_qualifying_stop(
        self,
        page: PageResult,
        qualifying: Callable[[Review], bool] | None,
        qualified: int,
        qualifying_limit: int | None,
    ) -> tuple[PageResult, int]:
        """Retain matches and stamp a safe filtered-limit stop when satisfied."""
        if qualifying is None:
            return page, qualified
        retained = [review for review in page.reviews if qualifying(review)]
        qualified += len(retained)
        page = PageResult(
            reviews=retained,
            next_cursor=page.next_cursor,
            error=page.error,
            stopped_because=page.stopped_because,
            skipped_reviews=page.skipped_reviews,
        )
        if qualifying_limit is not None and qualified >= qualifying_limit:
            page = with_stop_reason(
                page, prefer_stop_reason(page.stopped_because, "limit")
            )
        return page, qualified

    def _outcome_country(self, provider: ReviewProvider, country: str) -> str | None:
        """None for global providers, which have no country to report.

        Keyed off ``core.paging.is_per_country``, the same
        canonical convention ``_country_arg`` uses and ``resolve_countries``
        documents, rather than off the ``""`` sentinel value in hand.
        """
        return country if is_per_country(provider.source) else None

    def _assemble(
        self,
        collected: list[tuple[list[Review], CountryOutcome]],
        sort: Sort,
        limit: int | None,
    ) -> FetchResult:
        """Merge per-country walks into one filtered, sorted result."""
        reviews: list[Review] = []
        outcomes: list[CountryOutcome] = []
        for country_reviews, outcome in collected:
            reviews.extend(country_reviews)
            outcomes.append(outcome)

        return FetchResult(reviews=reviews, outcomes=outcomes).sort(sort).limit(limit)

    def _country_arg(
        self, provider: ReviewProvider, country: Country | str | None
    ) -> str:
        """Resolve the country argument for a single-country provider call.

        Global providers ignore it, so pass the ``""`` sentinel they expect.
        """
        if not is_per_country(provider.source):
            return ""
        return normalise_country(country) or self.DEFAULT_COUNTRY

    def _validate_country_argument(self, country: Country | str | None) -> None:
        """Subclass hook for stores whose review corpus has no country axis."""

    def _validate_countries_argument(
        self, countries: Collection[Country | str] | None
    ) -> None:
        """Plural twin of ``_validate_country_argument``."""

    def _country_list(
        self, provider: ReviewProvider, countries: Collection[Country | str] | None
    ) -> list[str]:
        """The fan-out ``_country_arg`` is the singular of: which countries to walk.

        Global sources collapse to a single ``[""]`` call. Same convention and
        same source of truth as ``_country_arg`` and ``_outcome_country``.

        Entries are normalised and deduped first. Every entry costs a full walk,
        so ``["us", "US", "USA"]`` left as-is would fetch one storefront three
        times and report it under three names in ``outcomes``. First-appearance
        order is kept, which matters because a frozenset region group has none
        of its own and callers do read ``outcomes`` positionally.
        """
        if not is_per_country(provider.source):
            if countries is not None:
                _LOG.warning(
                    "%s has no country dimension, so countries=%r is ignored; "
                    "one request already covers every territory",
                    provider.source,
                    list(countries),
                )
            return [""]
        if countries is None:
            return [self.DEFAULT_COUNTRY]
        return list(dict.fromkeys(countries))

    def _normalised_selection(
        self, countries: Collection[Country | str] | None
    ) -> list[str] | None:
        """Normalise an explicit selection while preserving omission vs empty."""
        if countries is None:
            return None
        resolved = (normalise_country(country) for country in countries)
        return list(dict.fromkeys(country for country in resolved if country))

    def _page_budget(self, max_pages: int | None) -> int:
        """Resolve and validate a per-country page request budget."""
        require_non_negative(max_pages, "max_pages")
        return self.MAX_PAGES if max_pages is None else max_pages
