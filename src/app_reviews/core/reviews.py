"""Base reviews client: the page/cursor ladder and the fetch pipeline."""

from __future__ import annotations

import abc
import asyncio
import contextlib
import dataclasses
import functools
import logging
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Collection,
    Iterator,
)
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from datetime import date, datetime
from typing import Any

from app_reviews.core.classify import fetch_error_from_exception
from app_reviews.core.client import PooledClient
from app_reviews.core.cursor import CursorScope, unwrap_cursor, wrap_cursor
from app_reviews.core.http import HttpClient
from app_reviews.core.paging import (
    DEFAULT_MAX_PAGES,
    CountryCollector,
    StopPolicy,
    is_per_country,
    orders_newest_first,
    plan_limits,
    prefer_stop_reason,
    with_stop_reason,
)
from app_reviews.core.provider import ReviewProvider
from app_reviews.core.ratelimit import RequestLimiter
from app_reviews.core.validation import require_non_negative, require_positive
from app_reviews.errors import AppReviewsError, AuthError
from app_reviews.models.config import RetryConfig
from app_reviews.models.country import Country, normalise_country
from app_reviews.models.page import PageResult
from app_reviews.models.result import (
    CountryOutcome,
    FetchResult,
    build_review_predicate,
)
from app_reviews.models.review import Review
from app_reviews.models.types import Sort, Source

_LOG = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class _WalkRequest:
    """Validated arguments shared by the multi-country rungs."""

    page_budget: int
    selection: list[str] | None
    """Normalised countries; None means the caller named none."""


@dataclasses.dataclass(frozen=True, slots=True)
class _FetchPlan:
    """Everything ``fetch`` decides before it walks the first country."""

    countries: list[str]
    since: date | datetime | None
    page_budget: int
    walk_limit: int | None
    """Fetched-review count that may end one country walk."""
    matches_filters: Callable[[Review], bool] | None
    matching_limit: int | None
    """Matching-review count that may end one country walk."""
    worker_count: int


def _failed_page(country: str, exc: AppReviewsError) -> PageResult:
    """A page carrying a raised package error, for the rungs that report errors.

    The sync and async page fetches both convert here, so the error a caller
    sees does not depend on which twin raised it. ``country`` is the provider
    argument, where ``""`` means a global source and reports as no country.
    """
    return PageResult(
        error=fetch_error_from_exception(country=country or None, exc=exc)
    )


def _first_failure(futures: list[Future[Any]]) -> BaseException | None:
    """The exception of the first finished future that failed, in submit order."""
    for future in futures:
        if future.done() and not future.cancelled() and future.exception():
            return future.exception()
    return None


def _country_arg(
    source: Source, country: Country | str | None, default_country: str
) -> str:
    """Resolve the country argument for a single-country provider call.

    Global sources ignore it, so pass the ``""`` sentinel they expect.
    """
    if not is_per_country(source):
        return ""
    return normalise_country(country) or default_country


def _country_list(
    source: Source,
    countries: Collection[Country | str] | None,
    default_country: str,
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
    if not is_per_country(source):
        if countries is not None:
            _LOG.warning(
                "%s has no country dimension, so countries=%r is ignored; "
                "one request already covers every territory",
                source,
                list(countries),
            )
        return [""]
    if countries is None:
        return [default_country]
    return list(dict.fromkeys(countries))


def _normalised_selection(
    countries: Collection[Country | str] | None,
) -> list[str] | None:
    """Normalise an explicit selection while preserving omission vs empty."""
    if countries is None:
        return None
    resolved = (normalise_country(country) for country in countries)
    return list(dict.fromkeys(country for country in resolved if country))


def _outcome_country(source: Source, country: str) -> str | None:
    """None for global sources, which have no country to report.

    Keyed off ``core.paging.is_per_country``, the same canonical convention
    ``_country_arg`` uses and ``resolve_countries`` documents, rather than
    off the ``""`` sentinel value in hand.
    """
    return country if is_per_country(source) else None


def _log_stream_error(page: PageResult, country: str) -> None:
    """Report a failed page on a streaming walk, which cannot return errors."""
    if page.error is not None:
        _LOG.warning(
            "iter_reviews: %s walk for %r failed: %s",
            page.error.kind,
            country or "global",
            page.error.message,
        )


def _report_and_select_reviews(
    page: PageResult,
    country: str,
    matches_since: Callable[[Review], bool] | None,
    on_page: Callable[[PageResult], None] | None,
) -> list[Review]:
    """One streamed page's side effects, then the reviews of it that pass ``since``.

    The error is logged and ``on_page`` called before any review of the page is
    yielded, whether or not the page has reviews.
    """
    _log_stream_error(page, country)
    if on_page is not None:
        on_page(page)
    if matches_since is None:
        return page.reviews
    return [review for review in page.reviews if matches_since(review)]


def _split_at_room(batch: list[Review], room: int | None) -> tuple[list[Review], bool]:
    """The reviews of ``batch`` that fit in ``room``, and whether any were left over."""
    if room is None:
        return batch, False
    return batch[:room], len(batch) > room


class _CountryWalk:
    """One country's pages as ``fetch`` sees them: filtered, then collected.

    The clock starts at construction, so build this immediately before draining
    the pages.
    """

    def __init__(self, country: str | None, plan: _FetchPlan) -> None:
        self._collector = CountryCollector(country)
        self._plan = plan
        self._matching_review_count = 0

    def drain(self, pages: Iterator[PageResult]) -> None:
        """Take pages until one ends the walk."""
        for page in pages:
            if self._step(page):
                return

    async def adrain(self, pages: AsyncIterator[PageResult]) -> None:
        """Async twin of ``drain``."""
        async for page in pages:
            if self._step(page):
                return

    def _step(self, page: PageResult) -> bool:
        """Fold one page in; True once it is the walk's last."""
        fetched_review_count = len(page.reviews)
        page = self._retain_matching_reviews(page)
        self._collector.add(page, reviews_fetched=fetched_review_count)
        return page.stopped_because is not None

    def reviews_and_outcome(self) -> tuple[list[Review], CountryOutcome]:
        """The collected reviews and the outcome summarising the walk."""
        return self._collector.reviews, self._collector.outcome()

    def _retain_matching_reviews(self, page: PageResult) -> PageResult:
        """Keep only matching reviews, and stamp ``limit`` once enough matched."""
        plan = self._plan
        if plan.matches_filters is None:
            return page
        matching_reviews = [
            review for review in page.reviews if plan.matches_filters(review)
        ]
        self._matching_review_count += len(matching_reviews)
        page = dataclasses.replace(page, reviews=matching_reviews)
        has_enough_matches = (
            plan.matching_limit is not None
            and self._matching_review_count >= plan.matching_limit
        )
        if has_enough_matches:
            page = with_stop_reason(
                page, prefer_stop_reason(page.stopped_because, "limit")
            )
        return page


class BaseReviews(PooledClient, abc.ABC):
    """Base reviews client.

    Four rungs, each built on the one below and mirrored sync/async:

    - ``fetch_page``: one provider page. Cursor in, cursor out. Persist the cursor
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

    **Subclass hooks.** A store's client supplies its providers and the few facts
    the ladder cannot derive from ``provider.source``:

    - ``_build_provider`` (abstract): return the ``ReviewProvider`` for this
      client's configuration, passing ``http=self._http`` so every request shares
      the client's pool, retry policy and rate limiter. May read credentials and
      block; ``source`` and the ladder call it at most once and cache the result.
    - ``_abuild_provider``: same contract from an async context. Defaults to
      ``_build_provider``; override when construction does blocking I/O (a key
      file read, a token exchange) so the event loop is not held.
    - ``_is_newest_first(provider)``: whether this client's pages arrive
      newest-first. Defaults to ``orders_newest_first(provider.source)``;
      override when the caller can change the order, since every early stop
      (``since``, a newest-first ``limit``) trusts the answer.
    - ``_validate_country_argument(country)`` and
      ``_validate_countries_argument(countries)``: raise ``ValueError`` for a
      country the store cannot honour, before any provider is built or I/O
      done. Default to accepting everything; override for stores with no
      country axis.
    - ``DEFAULT_COUNTRY``: the storefront walked when a per-country source is
      given none.
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
        rate_limiter: RequestLimiter | None = None,
    ) -> None:
        super().__init__(proxy=proxy, retry=retry, http=http, rate_limiter=rate_limiter)
        self._cached_provider: ReviewProvider | None = None

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
        not exist and a fan-out would be decorative: one global walk covers every
        territory. Keyed off ``core.paging.is_per_country``, which is the
        only home for that fact.
        """
        self._validate_countries_argument(countries)
        selection = _normalised_selection(countries)
        if selection == []:
            return []
        return _country_list(
            self._ensure_provider().source, selection, self.DEFAULT_COUNTRY
        )

    # ---- rung 1: one provider page ------------------------------------------

    def fetch_page(
        self,
        app_id: str,
        *,
        country: Country | str | None = None,
        cursor: str | None = None,
    ) -> PageResult:
        """Fetch exactly one page. No pagination, no filtering, no stop conditions.

        ``cursor`` is opaque. Persist it verbatim to resume later, with the same
        app, country and client settings: a cursor resumed under different ones
        raises ``ValueError`` before any request is sent. Cursors saved by
        version 1.3 are still accepted. A returned ``next_cursor`` of None means
        no more pages.
        ``stopped_because`` is always None here, because no page walk is being
        performed. Retries and provider fallbacks may make multiple HTTP requests.

        A failure is reported on ``.error``, never raised, except ``AuthError``:
        see the class docstring for why an unusable credential raises.
        """
        self._validate_country_argument(country)
        provider = self._ensure_provider()
        country_arg = _country_arg(provider.source, country, self.DEFAULT_COUNTRY)
        scope = self._make_cursor_scope(provider, app_id, country_arg)
        raw_cursor = _resume_cursor(cursor, scope)
        page = self._page(provider, app_id, country_arg, raw_cursor)
        return _wrap_next_cursor(page, scope)

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
        country_arg = _country_arg(provider.source, country, self.DEFAULT_COUNTRY)
        scope = self._make_cursor_scope(provider, app_id, country_arg)
        raw_cursor = _resume_cursor(cursor, scope)
        page = await self._apage(provider, app_id, country_arg, raw_cursor)
        return _wrap_next_cursor(page, scope)

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

        ``max_pages`` is the maximum number of provider pages in this country walk,
        not HTTP attempts. Zero performs no I/O; omission uses the safety ceiling.
        """
        page_budget = self._validate_page_walk(country, limit, max_pages)
        if page_budget is None:
            return
        provider = self._ensure_provider()
        country_arg = _country_arg(provider.source, country, self.DEFAULT_COUNTRY)
        scope = self._make_cursor_scope(provider, app_id, country_arg)
        raw_cursor = _resume_cursor(cursor, scope)
        yield from self._walk(
            provider,
            app_id,
            country_arg,
            raw_cursor,
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
        page_budget = self._validate_page_walk(country, limit, max_pages)
        if page_budget is None:
            return
        provider = await self._aensure_provider()
        country_arg = _country_arg(provider.source, country, self.DEFAULT_COUNTRY)
        scope = self._make_cursor_scope(provider, app_id, country_arg)
        raw_cursor = _resume_cursor(cursor, scope)
        walk = self._awalk(
            provider,
            app_id,
            country_arg,
            raw_cursor,
            since,
            limit,
            page_budget,
        )
        async with contextlib.aclosing(walk):
            async for page in walk:
                yield page

    def _validate_page_walk(
        self, country: Country | str | None, limit: int | None, max_pages: int | None
    ) -> int | None:
        """Validate ``iter_pages`` arguments; the page budget, or None for no I/O.

        A zero ``limit`` or a zero page budget returns before a provider is
        built, so it never authenticates.
        """
        self._validate_country_argument(country)
        require_non_negative(limit, "limit")
        page_budget = self._page_budget(max_pages)
        if limit == 0 or page_budget == 0:
            return None
        return page_budget

    def _new_stop_policy(
        self,
        provider: ReviewProvider,
        since: date | datetime | None,
        limit: int | None,
        cursor: str | None,
        max_pages: int,
    ) -> StopPolicy:
        """The stop policy of one country walk. It keeps count, so none is shared."""
        return StopPolicy(
            provider.source,
            since,
            limit,
            max_pages=max_pages,
            initial_cursor=cursor,
            is_newest_first=self._is_newest_first(provider),
        )

    def _cursor_options(self) -> tuple[str, ...]:
        """The client settings that change what a raw cursor means.

        Folded into the fingerprint of every cursor handed out, so a cursor
        resumed under other settings is refused. A client whose settings shape
        the pages a provider walks overrides this; the default has none.
        """
        return ()

    def _make_cursor_scope(
        self, provider: ReviewProvider, app_id: str, country: str
    ) -> CursorScope:
        """What the cursors of this app and country walk are valid for."""
        return CursorScope(
            provider.source, app_id, country, options=self._cursor_options()
        )

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
        policy = self._new_stop_policy(provider, since, limit, cursor, max_pages)
        scope = self._make_cursor_scope(provider, app_id, country)

        while True:
            page = self._page(provider, app_id, country, cursor)
            reason, next_cursor = policy.evaluate(page)
            yield with_stop_reason(_wrap_next_cursor(page, scope), reason)
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
    ) -> AsyncGenerator[PageResult, None]:
        """Async equivalent of ``_walk``.

        The loop body is written twice because an async generator cannot share
        one with a sync generator. The stop *policy* is not duplicated; both
        delegate to ``StopPolicy``.
        """
        policy = self._new_stop_policy(provider, since, limit, cursor, max_pages)
        scope = self._make_cursor_scope(provider, app_id, country)

        while True:
            page = await self._apage(provider, app_id, country, cursor)
            reason, next_cursor = policy.evaluate(page)
            yield with_stop_reason(_wrap_next_cursor(page, scope), reason)
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
        on_page: Callable[[str | None, PageResult], None] | None = None,
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
        nowhere to hand a ``FetchError`` back. Pass ``on_page`` to see it anyway:
        it is called with ``(country, page)`` for every page fetched, including
        empty and failed ones, before that page's reviews are yielded. ``country``
        is None for a global source; ``page.reviews`` counts what was fetched,
        before ``since`` and ``limit`` trim what is yielded. The callback runs
        inline, also on the async twin, so keep it quick; an exception it raises
        ends the stream.
        """
        request = self._validate_walk_request(countries, limit, max_pages)
        if request is None:
            return
        provider = self._ensure_provider()
        matches_since = build_review_predicate(since=since)
        yielded_count = 0

        for country in _country_list(
            provider.source, request.selection, self.DEFAULT_COUNTRY
        ):
            if limit is not None and yielded_count >= limit:
                return
            pages, room = self._open_stream_walk(
                provider,
                app_id,
                country,
                request.page_budget,
                since,
                limit,
                yielded_count,
            )
            country_on_page = self._bind_page_callback(on_page, provider, country)
            for review in self._iter_country_reviews(
                pages, country, matches_since, country_on_page, room
            ):
                yield review
                yielded_count += 1

    async def aiter_reviews(
        self,
        app_id: str,
        *,
        countries: Collection[Country | str] | None = None,
        since: date | datetime | None = None,
        limit: int | None = None,
        max_pages: int | None = None,
        on_page: Callable[[str | None, PageResult], None] | None = None,
    ) -> AsyncIterator[Review]:
        """Async equivalent of ``iter_reviews``, with identical semantics.

        Each country's ``country_reviews`` and the ``pages`` it reads from are
        closed explicitly, ``country_reviews`` first: an early exit leaves both
        suspended, and the event loop would only close them at some later point.
        """
        request = self._validate_walk_request(countries, limit, max_pages)
        if request is None:
            return
        provider = await self._aensure_provider()
        matches_since = build_review_predicate(since=since)
        yielded_count = 0

        for country in _country_list(
            provider.source, request.selection, self.DEFAULT_COUNTRY
        ):
            if limit is not None and yielded_count >= limit:
                return
            pages, room = await self._aopen_stream_walk(
                provider,
                app_id,
                country,
                request.page_budget,
                since,
                limit,
                yielded_count,
            )
            country_on_page = self._bind_page_callback(on_page, provider, country)
            country_reviews = self._aiter_country_reviews(
                pages, country, matches_since, country_on_page, room
            )
            async with contextlib.aclosing(pages), contextlib.aclosing(country_reviews):
                async for review in country_reviews:
                    yield review
                    yielded_count += 1

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
            return _failed_page(country, exc)

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
            return _failed_page(country, exc)

    def _open_stream_walk(
        self,
        provider: ReviewProvider,
        app_id: str,
        country: str,
        page_budget: int,
        since: date | datetime | None,
        limit: int | None,
        yielded_count: int,
    ) -> tuple[Iterator[PageResult], int | None]:
        """Start the walk of the next stream country: ``(pages, room)``.

        ``room`` is the reviews the caller still wants, counted in reviews
        yielded; None means no ``limit``. The walk's own limit counts reviews
        fetched, which is ``plan_limits``' stream limit.
        """
        walk_limit, room = self._stream_budget(provider, since, limit, yielded_count)
        pages = self._walk(
            provider,
            app_id,
            country,
            cursor=None,
            since=since,
            limit=walk_limit,
            max_pages=page_budget,
        )
        return pages, room

    async def _aopen_stream_walk(
        self,
        provider: ReviewProvider,
        app_id: str,
        country: str,
        page_budget: int,
        since: date | datetime | None,
        limit: int | None,
        yielded_count: int,
    ) -> tuple[AsyncGenerator[PageResult, None], int | None]:
        """Async twin of ``_open_stream_walk``."""
        walk_limit, room = self._stream_budget(provider, since, limit, yielded_count)
        pages = self._awalk(
            provider,
            app_id,
            country,
            cursor=None,
            since=since,
            limit=walk_limit,
            max_pages=page_budget,
        )
        return pages, room

    def _stream_budget(
        self,
        provider: ReviewProvider,
        since: date | datetime | None,
        limit: int | None,
        yielded_count: int,
    ) -> tuple[int | None, int | None]:
        """What the next stream country may fetch and yield, as ``(walk_limit, room)``.

        ``walk_limit`` is ``plan_limits``' stream limit, which ``StopPolicy``
        counts in reviews fetched. ``room`` is the reviews the caller still
        wants, counted in reviews yielded; None means no ``limit``.
        """
        # Only ``stream_limit`` is read, and it depends on neither the sort nor
        # the row filters, so those take their no-op values.
        walk_limit = plan_limits(
            is_newest_first=self._is_newest_first(provider),
            sort=Sort.NEWEST,
            limit=limit,
            ratings=None,
            until=None,
            since=since,
            yielded=yielded_count,
        ).stream_limit
        room = None if limit is None else limit - yielded_count
        return walk_limit, room

    def _bind_page_callback(
        self,
        on_page: Callable[[str | None, PageResult], None] | None,
        provider: ReviewProvider,
        country: str,
    ) -> Callable[[PageResult], None] | None:
        """``on_page`` with this walk's reported country already filled in."""
        if on_page is None:
            return None
        return functools.partial(on_page, _outcome_country(provider.source, country))

    def _iter_country_reviews(
        self,
        pages: Iterator[PageResult],
        country: str,
        matches_since: Callable[[Review], bool] | None,
        on_page: Callable[[PageResult], None] | None,
        room: int | None,
    ) -> Iterator[Review]:
        """One country's reviews, page by page, dropping those before ``since``.

        A streaming walk needs ``since`` twice. ``StopPolicy`` uses it to stop
        requesting pages, and it stops *on* the page whose oldest review predates
        the boundary: a page that still carries the newer reviews above it
        alongside the older ones below. ``fetch`` drops those through
        ``FetchResult.filter`` afterwards; a stream has nothing downstream to do
        it, so it filters as it yields.

        ``room`` is how many reviews the caller still wants, None for no limit.
        The stream ends on the first review past it, which is not yielded.
        """
        for page in pages:
            page_reviews = _report_and_select_reviews(
                page, country, matches_since, on_page
            )
            fitting_reviews, has_leftover = _split_at_room(page_reviews, room)
            yield from fitting_reviews
            if has_leftover:
                return
            room = None if room is None else room - len(fitting_reviews)

    async def _aiter_country_reviews(
        self,
        pages: AsyncIterator[PageResult],
        country: str,
        matches_since: Callable[[Review], bool] | None,
        on_page: Callable[[PageResult], None] | None,
        room: int | None,
    ) -> AsyncGenerator[Review, None]:
        """Async twin of ``_iter_country_reviews``."""
        async for page in pages:
            page_reviews = _report_and_select_reviews(
                page, country, matches_since, on_page
            )
            fitting_reviews, has_leftover = _split_at_room(page_reviews, room)
            for review in fitting_reviews:
                yield review
            if has_leftover:
                return
            room = None if room is None else room - len(fitting_reviews)

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
        walk so filtered fetches have an explicit provider-page budget.
        """
        request = self._validate_walk_request(
            countries, limit, max_pages, concurrency=concurrency
        )
        if request is None:
            return FetchResult()
        provider = self._ensure_provider()
        plan = self._build_fetch_plan(
            provider,
            request,
            since=since,
            until=until,
            ratings=ratings,
            sort=sort,
            limit=limit,
            concurrency=concurrency,
        )
        collected = self._collect_countries(provider, app_id, plan)
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
        request = self._validate_walk_request(
            countries, limit, max_pages, concurrency=concurrency
        )
        if request is None:
            return FetchResult()
        provider = await self._aensure_provider()
        plan = self._build_fetch_plan(
            provider,
            request,
            since=since,
            until=until,
            ratings=ratings,
            sort=sort,
            limit=limit,
            concurrency=concurrency,
        )
        collected = await self._acollect_countries(provider, app_id, plan)
        return self._assemble(collected, sort, limit)

    def _build_fetch_plan(
        self,
        provider: ReviewProvider,
        request: _WalkRequest,
        *,
        since: date | datetime | None,
        until: date | datetime | None,
        ratings: list[int] | None,
        sort: Sort,
        limit: int | None,
        concurrency: int | None,
    ) -> _FetchPlan:
        """Resolve the countries, limits, filter and fan-out width for one fetch."""
        countries = _country_list(
            provider.source, request.selection, self.DEFAULT_COUNTRY
        )
        limits = plan_limits(
            is_newest_first=self._is_newest_first(provider),
            sort=sort,
            limit=limit,
            ratings=ratings,
            until=until,
            since=since,
        )
        self._log_exhaustive_walk(provider, sort, limit, ratings=ratings, until=until)
        return _FetchPlan(
            countries=countries,
            since=since,
            page_budget=request.page_budget,
            walk_limit=limits.walk_limit,
            matches_filters=build_review_predicate(
                ratings=ratings, since=since, until=until
            ),
            matching_limit=limits.matching_limit,
            worker_count=self._workers(countries, concurrency),
        )

    def _collect_countries(
        self, provider: ReviewProvider, app_id: str, plan: _FetchPlan
    ) -> list[tuple[list[Review], CountryOutcome]]:
        """Walk every planned country, on a thread pool when more than one.

        The first failure stops the walks still queued: ``AuthError`` is the same
        for every country, so starting them only spends requests on a rejected
        credential. Walks already running are waited for, never abandoned.
        """
        if plan.worker_count == 1:
            return [
                self._walk_country(provider, app_id, country, plan)
                for country in plan.countries
            ]
        with ThreadPoolExecutor(max_workers=plan.worker_count) as pool:
            futures = [
                pool.submit(self._walk_country, provider, app_id, country, plan)
                for country in plan.countries
            ]
            wait(futures, return_when=FIRST_EXCEPTION)
            failure = _first_failure(futures)
            if failure is not None:
                pool.shutdown(wait=True, cancel_futures=True)
                raise failure
            return [future.result() for future in futures]

    async def _acollect_countries(
        self, provider: ReviewProvider, app_id: str, plan: _FetchPlan
    ) -> list[tuple[list[Review], CountryOutcome]]:
        """Async twin of ``_collect_countries``, bounded by a semaphore."""
        semaphore = asyncio.Semaphore(plan.worker_count)
        failures: list[Exception] = []
        # return_exceptions so one country's failure does not cancel the others
        # mid-flight, orphaning their requests on the shared pool. The threaded
        # sync twin waits for every running worker; this makes the async path
        # match. Walks that have not started check ``failures`` and skip.
        settled = await asyncio.gather(
            *(
                self._awalk_unless_failed(
                    provider, app_id, country, plan, semaphore, failures
                )
                for country in plan.countries
            ),
            return_exceptions=True,
        )
        if failures:
            raise failures[0]
        collected: list[tuple[list[Review], CountryOutcome]] = []
        for outcome in settled:
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                collected.append(outcome)
        return collected

    async def _awalk_unless_failed(
        self,
        provider: ReviewProvider,
        app_id: str,
        country: str,
        plan: _FetchPlan,
        semaphore: asyncio.Semaphore,
        failures: list[Exception],
    ) -> tuple[list[Review], CountryOutcome] | None:
        """Walk one country once a slot is free, or skip it after a failure.

        Only a walk that had to queue is skipped, like a job still waiting in the
        sync pool's queue: one that found a free slot is already running
        alongside the failing walk, as a pool worker would be. The check follows
        the semaphore so a queued walk sees failures recorded while it waited,
        and a failure is recorded before the slot is released, so the next queued
        walk is guaranteed to see it.
        """
        had_to_queue = semaphore.locked()
        async with semaphore:
            if had_to_queue and failures:
                return None
            try:
                return await self._awalk_country(provider, app_id, country, plan)
            except Exception as error:
                failures.append(error)
                raise

    def _log_exhaustive_walk(
        self,
        provider: ReviewProvider,
        sort: Sort,
        limit: int | None,
        *,
        ratings: list[int] | None,
        until: date | datetime | None,
    ) -> None:
        """Say why an unfiltered ``limit`` cannot stop the walk early.

        The decision is ``plan_limits``'; this only reports it. With
        ``ratings``/``until`` the walk is bounded by matches instead, so
        nothing is reported.
        """
        if limit is None or ratings is not None or until is not None:
            return
        order = Sort(sort)
        if order is not Sort.NEWEST:
            _LOG.info(
                "sort=%s with limit=%d requires exhausting pagination; "
                "only sort=newest can stop the walk early",
                order.value,
                limit,
            )
        elif not self._is_newest_first(provider):
            _LOG.info(
                "%s does not guarantee newest-first ordering, so limit=%d "
                "cannot stop the walk early",
                provider.source,
                limit,
            )

    def _is_newest_first(self, provider: ReviewProvider) -> bool:
        """Whether this client's pages arrive newest-first.

        Every early stop (``since``, a newest-first ``limit``) leans on this. A
        subclass that lets the caller reorder its source overrides it.
        """
        return orders_newest_first(provider.source)

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
        plan: _FetchPlan,
    ) -> tuple[list[Review], CountryOutcome]:
        """Drive rung 2 for one country and summarise what it did.

        Consumes ``_walk`` rather than re-implementing the page loop, so the
        stop conditions exist in exactly one place, and hands each page to a
        ``_CountryWalk`` so the accounting does too.
        """
        walk = _CountryWalk(_outcome_country(provider.source, country), plan)
        pages = self._walk(
            provider,
            app_id,
            country,
            cursor=None,
            since=plan.since,
            limit=plan.walk_limit,
            max_pages=plan.page_budget,
        )
        walk.drain(pages)
        return walk.reviews_and_outcome()

    async def _awalk_country(
        self,
        provider: ReviewProvider,
        app_id: str,
        country: str,
        plan: _FetchPlan,
    ) -> tuple[list[Review], CountryOutcome]:
        """Async equivalent of ``_walk_country``."""
        walk = _CountryWalk(_outcome_country(provider.source, country), plan)
        pages = self._awalk(
            provider,
            app_id,
            country,
            cursor=None,
            since=plan.since,
            limit=plan.walk_limit,
            max_pages=plan.page_budget,
        )
        async with contextlib.aclosing(pages):
            await walk.adrain(pages)
        return walk.reviews_and_outcome()

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

    def _validate_country_argument(self, country: Country | str | None) -> None:
        """Subclass hook for stores whose review corpus has no country axis."""

    def _validate_countries_argument(
        self, countries: Collection[Country | str] | None
    ) -> None:
        """Plural twin of ``_validate_country_argument``."""

    def _validate_walk_request(
        self,
        countries: Collection[Country | str] | None,
        limit: int | None,
        max_pages: int | None,
        *,
        concurrency: int | None = None,
    ) -> _WalkRequest | None:
        """Validate the multi-country arguments; None when they ask for no I/O.

        A zero ``limit``, a zero page budget and an explicitly empty country
        selection all return before a provider is built, so they never
        authenticate.
        """
        self._validate_countries_argument(countries)
        require_non_negative(limit, "limit")
        require_positive(concurrency, "concurrency")
        page_budget = self._page_budget(max_pages)
        selection = _normalised_selection(countries)
        if limit == 0 or page_budget == 0 or selection == []:
            return None
        return _WalkRequest(page_budget=page_budget, selection=selection)

    def _page_budget(self, max_pages: int | None) -> int:
        """Resolve and validate a per-country provider-page budget."""
        require_non_negative(max_pages, "max_pages")
        return self.MAX_PAGES if max_pages is None else max_pages


def _resume_cursor(cursor: str | None, scope: CursorScope) -> str | None:
    """The raw cursor to give the provider; raises when ``scope`` is not its own."""
    if cursor is None:
        return None
    return unwrap_cursor(cursor, scope)


def _wrap_next_cursor(page: PageResult, scope: CursorScope) -> PageResult:
    """The page as the caller sees it: its ``next_cursor`` tied to ``scope``."""
    if not page.next_cursor:
        return page
    return dataclasses.replace(page, next_cursor=wrap_cursor(page.next_cursor, scope))
