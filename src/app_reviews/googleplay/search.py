"""Client for searching and looking up Google Play apps."""

from __future__ import annotations

import html
import json
import logging
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from app_reviews.core.classify import raise_for_http_failure
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpResponse
from app_reviews.core.search import (
    aget_and_parse,
    get_and_parse,
    scraped_rating,
    scraped_rating_count,
    scraped_text,
)
from app_reviews.core.validation import require_non_negative
from app_reviews.errors import ParseError
from app_reviews.models.country import Country, normalise_country
from app_reviews.models.metadata import AppMetadata

if TYPE_CHECKING:
    from collections.abc import Iterator

_LOG = logging.getLogger(__name__)


class GooglePlaySearch(PooledClient):
    """Search and lookup for Google Play apps, by scraping the store pages.

    Satisfies ``SearchClient`` structurally; see that Protocol for the contract.

    Play publishes no search API, so both methods read the JSON that the store
    pages embed for their own JavaScript. That payload is a serialised protobuf:
    positional arrays, no field names, no published schema. Every index path
    below was read off a live response and can move without notice, which is why
    a page this cannot read is reported rather than answered.
    """

    SEARCH_URL = "https://play.google.com/store/search"
    DETAIL_URL = "https://play.google.com/store/apps/details"

    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
    """Sent on both requests, so the two present the same agent to Play.

    A hedge rather than a fix for an observed block: Play embedded the same data
    for the package's own default agent when this was measured.
    """

    VERSION = "Varies with device"
    """Fallback for an app that publishes no single version, which is what the
    store itself shows for one.

    Play does publish a version for *some* apps, at ``_DETAIL_VERSION``. Verified
    live against the ``us`` storefront: Firefox reports ``153.0.1`` there, while
    Spotify and Duolingo have nothing at that path, so it is genuinely per-app.
    Distinct from ``ds:10``, which carries a version per *review* (the build that
    reviewer was running, not the app's), and is not this.

    A search hit has no version field at all, so those keep the fallback.
    """

    SEARCH_DATASET = "ds:4"
    DETAIL_DATASET = "ds:5"

    _BLOCK = re.compile(r"AF_initDataCallback[\s\S]*?</script")
    _KEY = re.compile(r"(ds:\d+)'")
    _DATA = re.compile(r"\bdata:\s*")

    _SECTIONS = (0, 1)
    """In the search dataset."""

    _TOP_RESULT = (23, 16)
    _ENTRY_GROUPS = (22,)
    """In one section: the featured hit, and the groups of regular hits."""

    _DETAIL_BLOCK = (1, 2)
    _TOP_DETAIL_BLOCK = (2,)
    """Where a detail block sits: on a detail page, and inside a featured hit."""

    # A detail block. The featured search hit embeds one of these, so the detail
    # page and the top hit are read with the same paths.
    #
    # Google ships each number as a ``[display_string, value]`` pair, which is
    # why the rating paths end at slot 1: '4.0' is what a reader sees,
    # 3.9970803 is the number.
    _NAME = (0, 0)
    _STORE_URL = (41, 0, 2)
    _RATING = (51, 0, 1)
    _RATING_COUNT = (51, 2, 1)
    _PRICE = (57, 0, 0, 0, 0, 1, 0)
    _PRICE_MICROS_PER_UNIT = 1_000_000
    """Play sends prices in millionths of the currency unit."""
    _DEVELOPER = (68, 0)
    _CATEGORY = (79, 0, 0, 0)
    _ICON = (95, 0, 3, 2)
    _DETAIL_VERSION = (140, 0, 0, 0)
    """Absent for an app with per-device variants; see ``VERSION``."""

    _RELEASED_ON = (10, 0)
    _UPDATED_ON = (145, 0, 0)
    """The two dates Play renders as "Released on" and "Updated on".

    Display strings, not timestamps: ``Dec 21, 2010``. Day precision, and the month
    name is in the request's ``hl`` language, which is why ``hl`` is pinned to
    ``en`` on both requests. See ``_parse_display_date``.
    """

    _RELEASE_NOTES = (144, 1, 1)
    """The "What's new" text, with ``<br>`` for line breaks.

    See ``_parse_release_notes``.
    """

    _LINE_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)

    # A regular search hit: a smaller block, numbered independently of the detail
    # block above, and carrying no rating count.
    _ENTRY_BLOCK = (0,)
    _ENTRY_APP_ID = (0, 0)
    _ENTRY_ICON = (1, 3, 2)
    _ENTRY_NAME = (3,)
    _ENTRY_RATING = (4, 1)
    _ENTRY_CATEGORY = (5,)
    _ENTRY_PRICE = (8, 1, 1)
    _ENTRY_DEVELOPER = (14,)

    def search(
        self,
        query: str,
        *,
        country: Country | str = Country.US,
        limit: int = 50,
    ) -> list[AppMetadata]:
        """Search Play for apps matching ``query`` in ``country``'s market.

        One results page is read; ``limit`` truncates it and never fetches more,
        so asking for more apps than Play lists on that page returns what it lists.
        """
        require_non_negative(limit, "limit")
        if limit == 0:
            return []
        return get_and_parse(
            self._http,
            self.SEARCH_URL,
            self._search_params(query, country),
            self._parse_search,
            headers={"User-Agent": self.USER_AGENT},
        )[:limit]

    async def asearch(
        self,
        query: str,
        *,
        country: Country | str = Country.US,
        limit: int = 50,
    ) -> list[AppMetadata]:
        """Async equivalent of ``search``."""
        require_non_negative(limit, "limit")
        if limit == 0:
            return []
        return (
            await aget_and_parse(
                self._http,
                self.SEARCH_URL,
                self._search_params(query, country),
                self._parse_search,
                headers={"User-Agent": self.USER_AGENT},
            )
        )[:limit]

    def lookup(
        self,
        app_id: str,
        *,
        country: Country | str = Country.US,
    ) -> AppMetadata | None:
        """Look up an app by package name, or None if Play has no such app."""
        return get_and_parse(
            self._http,
            self.DETAIL_URL,
            self._lookup_params(app_id, country),
            lambda response: self._parse_lookup(response, app_id),
            headers={"User-Agent": self.USER_AGENT},
        )

    async def alookup(
        self,
        app_id: str,
        *,
        country: Country | str = Country.US,
    ) -> AppMetadata | None:
        """Async equivalent of ``lookup``."""
        return await aget_and_parse(
            self._http,
            self.DETAIL_URL,
            self._lookup_params(app_id, country),
            lambda response: self._parse_lookup(response, app_id),
            headers={"User-Agent": self.USER_AGENT},
        )

    def _search_params(self, query: str, country: Country | str) -> dict[str, str]:
        return {"q": query, "c": "apps", "hl": "en", "gl": self._market(country)}

    def _lookup_params(self, app_id: str, country: Country | str) -> dict[str, str]:
        return {"id": app_id, "hl": "en", "gl": self._market(country)}

    def _market(self, country: Country | str) -> str:
        """The alpha-2 market to query, defaulting to ``Country.US``.

        ``warn_unknown=False`` because ``Country`` is Apple's storefront
        list: Play serves markets it omits (Serbia, Bosnia, Morocco), and
        those reach ``gl`` as plain strings. Warning would flag a correct call.
        """
        return normalise_country(country, warn_unknown=False) or Country.US.value

    def _parse_search(self, response: HttpResponse) -> list[AppMetadata]:
        """Every app on the page, featured hit first.

        A page with a results section but nothing in it is genuinely empty. A
        page with no results section at all is unreadable, which is a different
        answer and must not arrive as ``[]``.
        """
        raise_for_http_failure(response, "Google Play search", credentialed=False)
        datasets = self._extract_datasets_or_raise(response)
        has_readable_results = False

        for dataset in self._order_datasets(datasets, self.SEARCH_DATASET):
            sections = self._at(dataset, self._SECTIONS)
            if not self._is_results_container(sections):
                continue
            has_readable_results = True
            if apps := self._collect_apps(sections):
                return apps

        if has_readable_results:
            return []
        raise ParseError(
            "Google Play returned a page with no search results section",
            status=response.status,
        )

    def _parse_lookup(self, response: HttpResponse, app_id: str) -> AppMetadata | None:
        """The app, or None when Play has no such package.

        Only a 404 means "no such app". A 200 this cannot read means the page
        moved, or Play served a consent or throttling page in its place, so it
        is reported, not answered with a placeholder app named "Unknown" whose
        rating is zero, which reads like a real and badly rated app.
        """
        if response.status == 404:
            return None
        raise_for_http_failure(response, "Google Play", credentialed=False)
        datasets = self._extract_datasets_or_raise(response)

        for dataset in self._order_datasets(datasets, self.DETAIL_DATASET):
            detail_block = self._at(dataset, self._DETAIL_BLOCK)
            app = self._parse_detail_app(detail_block, app_id)
            if app is not None:
                return app

        raise ParseError(
            f"Google Play returned a page with no app detail for {app_id}",
            status=response.status,
        )

    def _extract_datasets_or_raise(self, response: HttpResponse) -> dict[str, Any]:
        datasets = self._extract_datasets(response.body)
        if not datasets:
            raise ParseError(
                "Google Play returned a page with no embedded data",
                status=response.status,
            )
        return datasets

    def _extract_datasets(self, page_html: str) -> dict[str, Any]:
        """The page's ``AF_initDataCallback`` payloads, keyed by ``ds:N``.

        Play bootstraps its own JavaScript with blocks shaped like::

            AF_initDataCallback({key: 'ds:5', data:[...], sideChannel: {}});

        The array cannot be matched with a regex, being a megabyte of nested
        brackets, some of them inside strings. So ``raw_decode`` parses one JSON
        value starting where ``data:`` ends and stops when that value closes,
        which leaves the trailer after it alone.
        """
        datasets: dict[str, Any] = {}
        decoder = json.JSONDecoder()
        for block_match in self._BLOCK.finditer(page_html):
            block = block_match.group(0)
            key_match = self._KEY.search(block)
            data_start = self._DATA.search(block)
            if key_match is None or data_start is None:
                continue
            dataset_key = key_match.group(1)
            try:
                datasets[dataset_key], _ = decoder.raw_decode(block, data_start.end())
            except ValueError:
                _LOG.debug("Skipped an unreadable %s payload", dataset_key)
        return datasets

    def _order_datasets(
        self, datasets: dict[str, Any], preferred_key: str
    ) -> list[Any]:
        """Every dataset, the one that usually holds this page's data first.

        Google renumbers these keys between page versions, so a key is a hint
        rather than an address: try the expected one, then fall back to the rest.
        """
        ordered_keys = sorted(
            datasets,
            key=lambda dataset_key: (dataset_key != preferred_key, dataset_key),
        )
        return [datasets[dataset_key] for dataset_key in ordered_keys]

    def _is_results_container(self, sections: Any) -> bool:
        """Whether these sections are a search results container at all.

        A list at the sections path is not enough, because an unrelated dataset can have
        one, and accepting it reported an unreadable page as "no results". A real
        section carries a featured slot or a list of result groups.
        """
        if not isinstance(sections, list):
            return False
        for section in sections:
            if not isinstance(section, list):
                continue
            has_featured_hit = self._at(section, self._TOP_RESULT) is not None
            has_entry_groups = isinstance(self._at(section, self._ENTRY_GROUPS), list)
            if has_featured_hit or has_entry_groups:
                return True
        return False

    def _collect_apps(self, sections: list[Any]) -> list[AppMetadata]:
        """The apps across every section, the first mention of each id winning."""
        apps_by_id: dict[str, AppMetadata] = {}
        for section in sections:
            for app in self._iter_section_apps(section):
                apps_by_id.setdefault(app.app_id, app)
        return list(apps_by_id.values())

    def _iter_section_apps(self, section: Any) -> Iterator[AppMetadata]:
        """One section's apps: its featured hit, then its regular hits."""
        if not isinstance(section, list):
            return

        featured_app = self._parse_featured_app(self._at(section, self._TOP_RESULT))
        if featured_app is not None:
            yield featured_app

        groups = self._at(section, self._ENTRY_GROUPS)
        if not isinstance(groups, list):
            return
        for group in groups:
            yield from self._iter_group_apps(group)

    def _iter_group_apps(self, group: Any) -> Iterator[AppMetadata]:
        """The regular hits in one result group, skipping entries without an id."""
        if not isinstance(group, list):
            return
        for entry in group:
            app = self._parse_entry_app(entry)
            if app is not None:
                yield app

    def _parse_featured_app(self, entry: Any) -> AppMetadata | None:
        """The featured hit, which embeds a whole detail block.

        Its own id slot is empty in practice, so the package name is read out of
        the store link inside the block instead.
        """
        detail_block = self._at(entry, self._TOP_DETAIL_BLOCK)
        store_url = scraped_text(self._at(detail_block, self._STORE_URL))
        app_id = self._read_app_id_from_url(store_url)
        if app_id is None:
            return None
        return self._parse_detail_app(detail_block, app_id)

    def _parse_detail_app(self, block: Any, app_id: str) -> AppMetadata | None:
        """One app from a detail block, or None if the block carries no name.

        The name is what confirms the block is app detail at all: more than one
        dataset holds something list-shaped at the detail path, and only the real
        one names an app.
        """
        name = scraped_text(self._at(block, self._NAME))
        if name is None:
            return None

        return AppMetadata(
            app_id=app_id,
            store="googleplay",
            name=name,
            developer=scraped_text(self._at(block, self._DEVELOPER)) or "Unknown",
            category=scraped_text(self._at(block, self._CATEGORY)) or "Unknown",
            price=self._parse_price(self._at(block, self._PRICE)),
            version=scraped_text(self._at(block, self._DETAIL_VERSION)) or self.VERSION,
            rating=scraped_rating(self._at(block, self._RATING)),
            rating_count=scraped_rating_count(self._at(block, self._RATING_COUNT)),
            url=self._build_store_url(app_id),
            icon_url=scraped_text(self._at(block, self._ICON)),
            current_version_release_date=self._parse_display_date(
                self._at(block, self._UPDATED_ON)
            ),
            first_release_date=self._parse_display_date(
                self._at(block, self._RELEASED_ON)
            ),
            release_notes=self._parse_release_notes(
                self._at(block, self._RELEASE_NOTES)
            ),
        )

    def _parse_release_notes(self, value: Any) -> str | None:
        """Play's "What's new" as plain text, or None if the app shows none.

        Play renders it as HTML, so ``<br>`` becomes a newline and entities are
        decoded, matching the plain text the App Store sends.
        """
        text = scraped_text(value)
        if text is None:
            return None
        text_with_newlines = self._LINE_BREAK.sub("\n", text)
        return scraped_text(html.unescape(text_with_newlines))

    @staticmethod
    def _parse_display_date(value: Any) -> datetime | None:
        """One of Play's rendered dates as midnight UTC, or None if unreadable.

        Play publishes no timestamp for either date, only the day it prints on the
        page, so the time is padding rather than data; ``AppMetadata`` says so on
        the fields. Parsing is deliberately strict about the one format Play sends
        for ``hl=en``: a value in some other shape means the payload moved, and
        guessing at it would invent a date rather than report none.
        """
        text = scraped_text(value)
        if text is None:
            return None
        try:
            return datetime.strptime(text, "%b %d, %Y").replace(tzinfo=UTC)
        except ValueError:
            return None

    def _parse_entry_app(self, entry: Any) -> AppMetadata | None:
        """One regular search hit, or None without an id to key it by."""
        block = self._at(entry, self._ENTRY_BLOCK)
        app_id = scraped_text(self._at(block, self._ENTRY_APP_ID))
        if app_id is None:
            return None

        return AppMetadata(
            app_id=app_id,
            store="googleplay",
            name=scraped_text(self._at(block, self._ENTRY_NAME)) or "Unknown",
            developer=scraped_text(self._at(block, self._ENTRY_DEVELOPER)) or "Unknown",
            category=scraped_text(self._at(block, self._ENTRY_CATEGORY)) or "Unknown",
            price=self._parse_price(self._at(block, self._ENTRY_PRICE)),
            version=self.VERSION,
            rating=scraped_rating(self._at(block, self._ENTRY_RATING)),
            # This layout carries no count anywhere; only a detail block does.
            rating_count=0,
            url=self._build_store_url(app_id),
            icon_url=scraped_text(self._at(block, self._ENTRY_ICON)),
        )

    def _at(self, source: Any, path: tuple[int, ...]) -> Any:
        """The value at ``path``, or None if any step of it is missing.

        Every field is independently optional. The schema is undocumented, so a
        path that resolved yesterday can be absent today, and one missing field
        should cost that field rather than the whole app.
        """
        node = source
        for index in path:
            if not isinstance(node, list) or index >= len(node):
                return None
            node = node[index]
        return node

    def _build_store_url(self, app_id: str) -> str:
        return f"{self.DETAIL_URL}?id={app_id}"

    def _read_app_id_from_url(self, url: str | None) -> str | None:
        """The package name in a store link's ``id`` parameter."""
        if url is None:
            return None
        ids = parse_qs(urlsplit(url).query).get("id", [])
        if not ids or not ids[0]:
            return None
        return ids[0]

    def _parse_price(self, price: Any) -> str:
        """The storefront's display price, with an ISO-currency fallback.

        ``price`` is ``[micros, currency, display?]``. Absent price data means a
        free app; anything malformed is ``"Unknown"`` rather than a guess.
        """
        if price is None:
            return "Free"
        if not isinstance(price, list) or len(price) < 2:
            return "Unknown"

        amount = self._parse_price_micros(price[0])
        if amount is None:
            return "Unknown"
        if amount == 0:
            return "Free"

        display_price = self._read_display_price(price)
        if display_price is not None:
            return display_price
        return self._format_currency_price(price[1], amount)

    @staticmethod
    def _parse_price_micros(micros: Any) -> float | None:
        """The price in micros as a finite, non-negative number, or None."""
        if isinstance(micros, bool):
            return None
        try:
            amount = float(micros)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(amount) or amount < 0:
            return None
        return amount

    @staticmethod
    def _read_display_price(price: list[Any]) -> str | None:
        """The storefront's own formatted price, if Play sent one.

        Returns None when there is no display string, so the caller falls back
        to the currency code. A display string that looks negative is
        ``"Unknown"``: a paid app cannot cost less than nothing.
        """
        if len(price) <= 2 or not isinstance(price[2], str):
            return None
        formatted = price[2].strip()
        if not formatted:
            return None
        is_parenthesised = formatted.startswith("(") and formatted.endswith(")")
        if "-" in formatted or "\u2212" in formatted or is_parenthesised:
            return "Unknown"
        return formatted

    def _format_currency_price(self, currency: Any, amount: float) -> str:
        """``"USD 4.99"`` from the ISO currency code, or ``"Unknown"`` without one."""
        if not isinstance(currency, str) or not currency.strip():
            return "Unknown"
        units = amount / self._PRICE_MICROS_PER_UNIT
        return f"{currency.strip().upper()} {units:.2f}"
