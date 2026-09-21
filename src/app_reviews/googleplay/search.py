"""Client for searching and looking up Google Play apps."""

from __future__ import annotations

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
    ``en`` on both requests. See ``_display_date``.
    """

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
        datasets = self._datasets_or_raise(response)
        readable = False

        for data in self._preferring(datasets, self.SEARCH_DATASET):
            sections = self._at(data, self._SECTIONS)
            if not isinstance(sections, list) or not self._holds_results(sections):
                continue
            readable = True
            if apps := self._apps(sections):
                return apps

        if readable:
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
        datasets = self._datasets_or_raise(response)

        for data in self._preferring(datasets, self.DETAIL_DATASET):
            app = self._detail_app(self._at(data, self._DETAIL_BLOCK), app_id)
            if app is not None:
                return app

        raise ParseError(
            f"Google Play returned a page with no app detail for {app_id}",
            status=response.status,
        )

    def _datasets_or_raise(self, response: HttpResponse) -> dict[str, Any]:
        datasets = self._datasets(response.body)
        if not datasets:
            raise ParseError(
                "Google Play returned a page with no embedded data",
                status=response.status,
            )
        return datasets

    def _datasets(self, html: str) -> dict[str, Any]:
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
        for match in self._BLOCK.finditer(html):
            block = match.group(0)
            key = self._KEY.search(block)
            start = self._DATA.search(block)
            if key is None or start is None:
                continue
            try:
                datasets[key.group(1)], _ = decoder.raw_decode(block, start.end())
            except ValueError:
                _LOG.debug("Skipped an unreadable %s payload", key.group(1))
        return datasets

    def _preferring(self, datasets: dict[str, Any], key: str) -> list[Any]:
        """Every dataset, the one that usually holds this page's data first.

        Google renumbers these keys between page versions, so a key is a hint
        rather than an address: try the expected one, then fall back to the rest.
        """
        return [datasets[k] for k in sorted(datasets, key=lambda k: (k != key, k))]

    def _holds_results(self, sections: list[Any]) -> bool:
        """Whether these sections are a search results container at all.

        A list at the sections path is not enough, because an unrelated dataset can have
        one, and accepting it reported an unreadable page as "no results". A real
        section carries a featured slot or a list of result groups.
        """
        return any(
            isinstance(section, list)
            and (
                self._at(section, self._TOP_RESULT) is not None
                or isinstance(self._at(section, self._ENTRY_GROUPS), list)
            )
            for section in sections
        )

    def _apps(self, sections: list[Any]) -> list[AppMetadata]:
        """The apps across every section, the first mention of each id winning."""
        found: dict[str, AppMetadata] = {}
        for section in sections:
            for app in self._section_apps(section):
                found.setdefault(app.app_id, app)
        return list(found.values())

    def _section_apps(self, section: Any) -> Iterator[AppMetadata]:
        """One section's apps: its featured hit, then its regular hits."""
        if not isinstance(section, list):
            return

        top = self._top_app(self._at(section, self._TOP_RESULT))
        if top is not None:
            yield top

        groups = self._at(section, self._ENTRY_GROUPS)
        if not isinstance(groups, list):
            return
        for group in groups:
            if not isinstance(group, list):
                continue
            for entry in group:
                app = self._entry_app(entry)
                if app is not None:
                    yield app

    def _top_app(self, entry: Any) -> AppMetadata | None:
        """The featured hit, which embeds a whole detail block.

        Its own id slot is empty in practice, so the package name is read out of
        the store link inside the block instead.
        """
        block = self._at(entry, self._TOP_DETAIL_BLOCK)
        app_id = self._app_id_from_url(scraped_text(self._at(block, self._STORE_URL)))
        if app_id is None:
            return None
        return self._detail_app(block, app_id)

    def _detail_app(self, block: Any, app_id: str) -> AppMetadata | None:
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
            price=self._price(self._at(block, self._PRICE)),
            version=scraped_text(self._at(block, self._DETAIL_VERSION)) or self.VERSION,
            rating=scraped_rating(self._at(block, self._RATING)),
            rating_count=scraped_rating_count(self._at(block, self._RATING_COUNT)),
            url=self._url(app_id),
            icon_url=scraped_text(self._at(block, self._ICON)),
            current_version_release_date=self._display_date(
                self._at(block, self._UPDATED_ON)
            ),
            first_release_date=self._display_date(self._at(block, self._RELEASED_ON)),
        )

    @staticmethod
    def _display_date(value: Any) -> datetime | None:
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

    def _entry_app(self, entry: Any) -> AppMetadata | None:
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
            price=self._price(self._at(block, self._ENTRY_PRICE)),
            version=self.VERSION,
            rating=scraped_rating(self._at(block, self._ENTRY_RATING)),
            # This layout carries no count anywhere; only a detail block does.
            rating_count=0,
            url=self._url(app_id),
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

    def _url(self, app_id: str) -> str:
        return f"{self.DETAIL_URL}?id={app_id}"

    def _app_id_from_url(self, url: str | None) -> str | None:
        """The package name in a store link's ``id`` parameter."""
        if url is None:
            return None
        ids = parse_qs(urlsplit(url).query).get("id", [])
        return ids[0] if ids and ids[0] else None

    def _price(self, price: Any) -> str:
        """The storefront's display price, with an ISO-currency fallback."""
        if price is None:
            return "Free"
        if not isinstance(price, list) or len(price) < 2:
            return "Unknown"
        try:
            micros = price[0]
            if isinstance(micros, bool):
                return "Unknown"
            amount = float(micros)
            if not math.isfinite(amount):
                return "Unknown"
            if amount < 0:
                return "Unknown"
            if amount == 0:
                return "Free"
            if len(price) > 2 and isinstance(price[2], str) and price[2].strip():
                formatted = price[2].strip()
                if (
                    "-" in formatted
                    or "\u2212" in formatted
                    or (formatted.startswith("(") and formatted.endswith(")"))
                ):
                    return "Unknown"
                return formatted
            currency = price[1]
            if not isinstance(currency, str) or not currency.strip():
                return "Unknown"
            return f"{currency.strip().upper()} {amount / 1_000_000:.2f}"
        except (TypeError, ValueError):
            return "Unknown"
