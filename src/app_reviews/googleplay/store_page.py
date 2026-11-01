"""Reads the JSON that Google Play's store pages embed for their own JavaScript.

Play publishes no search API. That payload is a serialised protobuf: positional
arrays, no field names, no published schema. Every index path below was read off
a live response and can move without notice, which is why a page this cannot
read is reported rather than answered.

Scraped, undocumented page structure: best-effort, and may break when Google
changes the page. The functions here are pure; fetching lives in ``search.py``.
"""

from __future__ import annotations

import html
import logging
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlsplit

from app_reviews.core import safe_json
from app_reviews.core.http import HttpResponse
from app_reviews.core.scraped import (
    UNKNOWN,
    english_month_number,
    scraped_rating,
    scraped_rating_count,
    scraped_text,
)
from app_reviews.errors import ParseError
from app_reviews.models.metadata import AppMetadata

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "DETAIL_DATASET",
    "DETAIL_URL",
    "SEARCH_DATASET",
    "UNVERSIONED",
    "extract_datasets",
    "parse_detail_page",
    "parse_search_page",
]

_LOG = logging.getLogger(__name__)

DETAIL_URL = "https://play.google.com/store/apps/details"

UNVERSIONED = "Varies with device"
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

_BLOCK_START = "AF_initDataCallback"
_BLOCK_END = "</script"
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
"""Absent for an app with per-device variants; see ``UNVERSIONED``."""

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
_DISPLAY_DATE = re.compile(r"(?P<month>[A-Za-z]+)\s+(?P<day_and_year>\d{1,2},\s+\d{4})")
"""``Dec 21, 2010``, split so the English month name is read by hand."""

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


def parse_search_page(response: HttpResponse) -> list[AppMetadata]:
    """Every app on the search page, featured hit first.

    A page with a results section but nothing in it is genuinely empty. A
    page with no results section at all is unreadable, which is a different
    answer and must not arrive as ``[]``. Neither is a section holding entries
    that all fail to parse: that is a moved layout, not a query without
    matches. Every dataset is tried before deciding, since Google renumbers
    them.
    """
    datasets = _extract_datasets_or_raise(response)
    containers = _find_results_containers(datasets)

    for sections in containers:
        if apps := _collect_apps(sections):
            return apps
    if any(_has_result_candidates(sections) for sections in containers):
        raise ParseError(
            "Google Play returned search results with no usable apps",
            status=response.status,
        )
    if containers:
        return []
    raise ParseError(
        "Google Play returned a page with no search results section",
        status=response.status,
    )


def parse_detail_page(response: HttpResponse, app_id: str) -> AppMetadata:
    """The app on its detail page; ``ParseError`` when the page names none.

    A 200 this cannot read means the page moved, or Play served a consent or
    throttling page in its place, so it is reported, not answered with a
    placeholder app named "Unknown" whose rating is zero, which reads like a
    real and badly rated app.
    """
    datasets = _extract_datasets_or_raise(response)

    for dataset in _order_datasets(datasets, DETAIL_DATASET):
        app = _parse_detail_app(_at(dataset, _DETAIL_BLOCK), app_id)
        if app is not None:
            return app

    raise ParseError(
        f"Google Play returned a page with no app detail for {app_id}",
        status=response.status,
    )


def _extract_datasets_or_raise(response: HttpResponse) -> dict[str, Any]:
    datasets = extract_datasets(response.body)
    if not datasets:
        raise ParseError(
            "Google Play returned a page with no embedded data",
            status=response.status,
        )
    return datasets


def extract_datasets(page_html: str) -> dict[str, Any]:
    """The page's ``AF_initDataCallback`` payloads, keyed by ``ds:N``.

    Play bootstraps its own JavaScript with blocks shaped like::

        AF_initDataCallback({key: 'ds:5', data:[...], sideChannel: {}});

    The array cannot be matched with a regex, being a megabyte of nested
    brackets, some of them inside strings. So ``raw_decode`` parses one JSON
    value starting where ``data:`` ends and stops when that value closes,
    which leaves the trailer after it alone.
    """
    datasets: dict[str, Any] = {}
    for block in _iter_blocks(page_html):
        key_match = _KEY.search(block)
        data_start = _DATA.search(block)
        if key_match is None or data_start is None:
            continue
        dataset_key = key_match.group(1)
        try:
            datasets[dataset_key], _ = safe_json.raw_decode(block, data_start.end())
        except ValueError:
            _LOG.debug("Skipped an unreadable %s payload", dataset_key)
    return datasets


def _iter_blocks(page_html: str) -> Iterator[str]:
    """Each ``AF_initDataCallback`` block, up to and including its ``</script``.

    Found with ``str.find`` rather than ``AF_initDataCallback[\\s\\S]*?</script``:
    with no closing tag that lazy match rescans the rest of the page from
    every marker, so a page of repeated markers took quadratic time. Without
    a closing tag after one marker there is none after the next, so the scan
    stops.
    """
    position = 0
    while (start := page_html.find(_BLOCK_START, position)) != -1:
        closing = page_html.find(_BLOCK_END, start)
        if closing == -1:
            return
        position = closing + len(_BLOCK_END)
        yield page_html[start:position]


def _order_datasets(datasets: dict[str, Any], preferred_key: str) -> list[Any]:
    """Every dataset, the one that usually holds this page's data first.

    Google renumbers these keys between page versions, so a key is a hint
    rather than an address: try the expected one, then fall back to the rest.
    """
    ordered_keys = sorted(
        datasets,
        key=lambda dataset_key: (dataset_key != preferred_key, dataset_key),
    )
    return [datasets[dataset_key] for dataset_key in ordered_keys]


def _find_results_containers(datasets: dict[str, Any]) -> list[list[Any]]:
    """The sections of every dataset that is a search results container."""
    containers: list[list[Any]] = []
    for dataset in _order_datasets(datasets, SEARCH_DATASET):
        sections = _at(dataset, _SECTIONS)
        if _is_results_container(sections):
            containers.append(sections)
    return containers


def _is_results_container(sections: Any) -> bool:
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
        has_featured_hit = _at(section, _TOP_RESULT) is not None
        has_entry_groups = isinstance(_at(section, _ENTRY_GROUPS), list)
        if has_featured_hit or has_entry_groups:
            return True
    return False


def _has_result_candidates(sections: list[Any]) -> bool:
    """Whether any section holds a featured hit or an entry, usable or not."""
    for section in sections:
        if not isinstance(section, list):
            continue
        if _at(section, _TOP_RESULT) is not None:
            return True
        if _has_populated_group(_at(section, _ENTRY_GROUPS)):
            return True
    return False


def _has_populated_group(groups: Any) -> bool:
    """Whether any result group holds something; ``[]`` and ``[[]]`` do not."""
    if not isinstance(groups, list):
        return False
    return any(group not in (None, []) for group in groups)


def _collect_apps(sections: list[Any]) -> list[AppMetadata]:
    """The apps across every section, the first mention of each id winning."""
    apps_by_id: dict[str, AppMetadata] = {}
    for section in sections:
        for app in _iter_section_apps(section):
            apps_by_id.setdefault(app.app_id, app)
    return list(apps_by_id.values())


def _iter_section_apps(section: Any) -> Iterator[AppMetadata]:
    """One section's apps: its featured hit, then its regular hits."""
    if not isinstance(section, list):
        return

    featured_app = _parse_featured_app(_at(section, _TOP_RESULT))
    if featured_app is not None:
        yield featured_app

    groups = _at(section, _ENTRY_GROUPS)
    if not isinstance(groups, list):
        return
    for group in groups:
        yield from _iter_group_apps(group)


def _iter_group_apps(group: Any) -> Iterator[AppMetadata]:
    """The regular hits in one result group, skipping entries without an id."""
    if not isinstance(group, list):
        return
    for entry in group:
        app = _parse_entry_app(entry)
        if app is not None:
            yield app


def _parse_featured_app(entry: Any) -> AppMetadata | None:
    """The featured hit, which embeds a whole detail block.

    Its own id slot is empty in practice, so the package name is read out of
    the store link inside the block instead.
    """
    detail_block = _at(entry, _TOP_DETAIL_BLOCK)
    store_url = scraped_text(_at(detail_block, _STORE_URL))
    app_id = _read_app_id_from_url(store_url)
    if app_id is None:
        return None
    return _parse_detail_app(detail_block, app_id)


def _parse_detail_app(block: Any, app_id: str) -> AppMetadata | None:
    """One app from a detail block, or None if the block carries no name.

    The name is what confirms the block is app detail at all: more than one
    dataset holds something list-shaped at the detail path, and only the real
    one names an app.
    """
    name = scraped_text(_at(block, _NAME))
    if name is None:
        return None

    return AppMetadata(
        app_id=app_id,
        store="googleplay",
        name=name,
        developer=scraped_text(_at(block, _DEVELOPER)) or UNKNOWN,
        category=scraped_text(_at(block, _CATEGORY)) or UNKNOWN,
        price=_parse_price(_at(block, _PRICE)),
        version=scraped_text(_at(block, _DETAIL_VERSION)) or UNVERSIONED,
        rating=scraped_rating(_at(block, _RATING)),
        rating_count=scraped_rating_count(_at(block, _RATING_COUNT)),
        url=_build_store_url(app_id),
        icon_url=scraped_text(_at(block, _ICON)),
        current_version_release_date=_parse_display_date(_at(block, _UPDATED_ON)),
        first_release_date=_parse_display_date(_at(block, _RELEASED_ON)),
        release_notes=_parse_release_notes(_at(block, _RELEASE_NOTES)),
    )


def _parse_release_notes(value: Any) -> str | None:
    """Play's "What's new" as plain text, or None if the app shows none.

    Play renders it as HTML, so ``<br>`` becomes a newline and entities are
    decoded, matching the plain text the App Store sends.
    """
    text = scraped_text(value)
    if text is None:
        return None
    text_with_newlines = _LINE_BREAK.sub("\n", text)
    return scraped_text(html.unescape(text_with_newlines))


def _parse_display_date(value: Any) -> datetime | None:
    """One of Play's rendered dates as midnight UTC, or None if unreadable.

    Play publishes no timestamp for either date, only the day it prints on the
    page, so the time is padding rather than data; ``AppMetadata`` says so on
    the fields. Parsing is deliberately strict about the one format Play sends
    for ``hl=en``: a value in some other shape means the payload moved, and
    guessing at it would invent a date rather than report none.

    The month name is English because ``hl`` is, so it is looked up by hand
    rather than through ``%b``, which follows the process locale.
    """
    text = scraped_text(value)
    if text is None:
        return None
    match = _DISPLAY_DATE.fullmatch(text)
    if match is None:
        return None
    month = english_month_number(match["month"])
    if month is None:
        return None
    try:
        parsed = datetime.strptime(f"{month} {match['day_and_year']}", "%m %d, %Y")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def _parse_entry_app(entry: Any) -> AppMetadata | None:
    """One regular search hit, or None without an id to key it by."""
    block = _at(entry, _ENTRY_BLOCK)
    app_id = scraped_text(_at(block, _ENTRY_APP_ID))
    if app_id is None:
        return None

    return AppMetadata(
        app_id=app_id,
        store="googleplay",
        name=scraped_text(_at(block, _ENTRY_NAME)) or UNKNOWN,
        developer=scraped_text(_at(block, _ENTRY_DEVELOPER)) or UNKNOWN,
        category=scraped_text(_at(block, _ENTRY_CATEGORY)) or UNKNOWN,
        price=_parse_price(_at(block, _ENTRY_PRICE)),
        version=UNVERSIONED,
        rating=scraped_rating(_at(block, _ENTRY_RATING)),
        # This layout carries no count anywhere; only a detail block does.
        rating_count=0,
        url=_build_store_url(app_id),
        icon_url=scraped_text(_at(block, _ENTRY_ICON)),
    )


def _at(source: Any, path: tuple[int, ...]) -> Any:
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


def _build_store_url(app_id: str) -> str:
    return f"{DETAIL_URL}?id={quote(app_id, safe='')}"


def _read_app_id_from_url(url: str | None) -> str | None:
    """The package name in a store link's ``id`` parameter.

    None for a link that does not parse: ``urlsplit`` raises on a malformed
    IPv6 literal, and one bad link must cost only its own result.
    """
    if url is None:
        return None
    try:
        query = urlsplit(url).query
    except ValueError:
        return None
    ids = parse_qs(query).get("id", [])
    if not ids or not ids[0]:
        return None
    return ids[0]


def _parse_price(price: Any) -> str:
    """The storefront's display price, with an ISO-currency fallback.

    ``price`` is ``[micros, currency, display?]``. Absent price data means a
    free app; anything malformed is ``"Unknown"`` rather than a guess.
    """
    if price is None:
        return "Free"
    if not isinstance(price, list) or len(price) < 2:
        return UNKNOWN

    amount = _parse_price_micros(price[0])
    if amount is None:
        return UNKNOWN
    if amount == 0:
        return "Free"

    display_price = _read_display_price(price)
    if display_price is not None:
        return display_price
    return _format_currency_price(price[1], amount)


def _parse_price_micros(micros: Any) -> float | None:
    """The price in micros as a finite, non-negative number, or None."""
    if isinstance(micros, bool):
        return None
    try:
        amount = float(micros)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(amount) or amount < 0:
        return None
    return amount


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
        return UNKNOWN
    return formatted


def _format_currency_price(currency: Any, amount: float) -> str:
    """``"USD 4.99"`` from the ISO currency code, or ``"Unknown"`` without one."""
    if not isinstance(currency, str) or not currency.strip():
        return UNKNOWN
    units = amount / _PRICE_MICROS_PER_UNIT
    return f"{currency.strip().upper()} {units:.2f}"
