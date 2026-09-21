"""Tests for GooglePlaySearch."""

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app_reviews.core.http import HttpClient
from app_reviews.errors import HttpError, ParseError, RequestError
from app_reviews.googleplay.search import GooglePlaySearch
from app_reviews.models.config import RetryConfig
from app_reviews.models.country import Country
from app_reviews.models.metadata import AppMetadata

_ICON = "https://play-lh.googleusercontent.com/icon1.png"


def _page(key: str, data: Any) -> str:
    """One Play page carrying a single AF_initDataCallback dataset."""
    return (
        "<html><body>"
        f"<script>AF_initDataCallback({{key: '{key}', hash: '4', "
        "data:" + json.dumps(data) + ", "
        "sideChannel: {}});</script>"
        "</body></html>"
    )


def _detail_block(
    *,
    app_id: str = "com.whatsapp",
    name: Any = "WhatsApp Messenger",
    rating: Any = 4.7,
    rating_count: Any = 232000,
    price_micros: Any = 0,
    currency: Any = "USD",
    formatted_price: Any = None,
    version: Any = None,
    released_on: Any = None,
    updated_on: Any = None,
) -> list[Any]:
    """A detail block, as found at ds:5[1][2] and inside a top search result.

    ``version`` defaults to absent, which is the common case: most large apps ship
    per-device variants and publish no single version. Firefox does publish one,
    verified live at ``ds:5[1][2][140][0][0][0]``.

    ``released_on`` and ``updated_on`` are the display dates Play renders as
    "Released on" and "Updated on", verified live at ``[10][0]`` and ``[145][0][0]``.
    """
    block: list[Any] = [None] * 146
    block[0] = [name]
    if version is not None:
        block[140] = [[[version]]]
    if released_on is not None:
        block[10] = [released_on]
    if updated_on is not None:
        block[145] = [[updated_on]]
    block[41] = [
        [None, None, f"https://play.google.com/store/apps/details?id={app_id}"]
    ]
    block[51] = [[None, rating], None, [None, rating_count]]
    price = [price_micros, currency]
    if formatted_price is not None:
        price.append(formatted_price)
    block[57] = [[[[[None, [price]]]]]]
    block[68] = ["WhatsApp LLC"]
    block[79] = [[["Communication", None, "COMMUNICATION"]]]
    block[95] = [[None, None, None, [None, None, _ICON]]]
    return block


def _detail_page(**kwargs: Any) -> str:
    return _page("ds:5", [None, [None, None, _detail_block(**kwargs)]])


def _entry(
    *,
    app_id: str = "com.whatsapp",
    name: Any = "WhatsApp Messenger",
    rating: Any = 4.7,
    price_micros: Any = 0,
    currency: Any = "USD",
    formatted_price: Any = None,
) -> list[Any]:
    """A compact regular search entry: its own numbering, fewer fields."""
    app: list[Any] = [None] * 15
    app[0] = [app_id, 7]
    app[1] = [None, 2, [512, 512], [None, None, _ICON]]
    app[3] = name
    app[4] = [None, rating]
    app[5] = "Communication"
    price = [price_micros, currency]
    if formatted_price is not None:
        price.append(formatted_price)
    app[8] = [None, [None, price]]
    app[14] = "WhatsApp LLC"
    return [app]


def _search_page(
    *, groups: list[Any] | None = None, top: list[Any] | None = None
) -> str:
    """A search page: regular entries in section[22], a featured hit at [23][16]."""
    section: list[Any] = [None] * 24
    section[22] = groups if groups is not None else [[_entry()]]
    if top is not None:
        slot: list[Any] = [None] * 17
        slot[16] = top
        section[23] = slot
    return _page("ds:4", [[None, [None, section]]])


def _client(handler):
    # max_retries=0: these tests assert on the first failure, not on retry
    # behaviour (covered by tests/app_reviews/core/test_http.py); the default
    # RetryConfig would sleep through real backoff on every failure test.
    return GooglePlaySearch(
        http=HttpClient(
            transport=httpx.MockTransport(handler),
            retry=RetryConfig(max_retries=0),
        )
    )


def _serving(text: str, status: int = 200):
    return _client(lambda request: httpx.Response(status, text=text))


class TestConstruction:
    def test_default(self) -> None:
        assert GooglePlaySearch() is not None

    def test_with_proxy(self) -> None:
        client = GooglePlaySearch(proxy="http://proxy:8080")
        assert client._http._proxy == "http://proxy:8080"


class TestDatasets:
    def test_extracts_keyed_datasets(self) -> None:
        assert "ds:4" in GooglePlaySearch()._datasets(_search_page())

    def test_multiple_datasets(self) -> None:
        html = (
            "<script>AF_initDataCallback({key: 'ds:3', "
            "data:[1], sideChannel: {}});</script>"
            "<script>AF_initDataCallback({key: 'ds:5', "
            "data:[2], sideChannel: {}});</script>"
        )
        assert GooglePlaySearch()._datasets(html) == {"ds:3": [1], "ds:5": [2]}

    def test_no_callbacks_returns_empty(self) -> None:
        assert GooglePlaySearch()._datasets("<html>no data</html>") == {}

    def test_malformed_json_skipped(self) -> None:
        html = (
            "<script>AF_initDataCallback({key: 'ds:0', "
            "data:INVALID, sideChannel: {}});</script>"
        )
        assert GooglePlaySearch()._datasets(html) == {}

    def test_stops_at_the_end_of_the_json_value(self) -> None:
        """raw_decode must ignore the sideChannel trailer after the array."""
        assert GooglePlaySearch()._datasets(_page("ds:9", [1, [2, 3]])) == {
            "ds:9": [1, [2, 3]]
        }


class TestSearch:
    def test_returns_app_metadata(self) -> None:
        results = _serving(_search_page()).search("whatsapp")
        assert len(results) == 1
        assert isinstance(results[0], AppMetadata)

    def test_maps_entry_fields(self) -> None:
        app = _serving(_search_page()).search("whatsapp")[0]
        assert app.app_id == "com.whatsapp"
        assert app.store == "googleplay"
        assert app.name == "WhatsApp Messenger"
        assert app.developer == "WhatsApp LLC"
        assert app.category == "Communication"
        assert app.price == "Free"
        assert app.rating == 4.7
        assert app.icon_url == _ICON
        assert app.url == "https://play.google.com/store/apps/details?id=com.whatsapp"

    def test_sends_query_params(self) -> None:
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_search_page(groups=[]))

        _client(handler).search("whatsapp")
        assert "q=whatsapp" in seen["url"]
        assert "c=apps" in seen["url"]

    def test_sends_the_requested_country_as_gl(self) -> None:
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_search_page(groups=[]))

        _client(handler).search("whatsapp", country=Country.JP)
        assert "gl=jp" in seen["url"]

    def test_accepts_a_play_market_outside_the_itunes_enum(self, caplog) -> None:
        """The enum is Apple's 155 storefronts; Play serves markets it omits.

        Serbia is a real Play market with no iTunes storefront, so it can only
        be reached as a plain string. Passing one must not warn: "unrecognised
        storefront" is an iTunes question, and Play callers opt out of it.
        """
        import logging

        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_search_page(groups=[]))

        with caplog.at_level(logging.WARNING):
            _client(handler).search("whatsapp", country="rs")

        assert "gl=rs" in seen["url"]
        assert "Unrecognised storefront" not in caplog.text

    def test_normalises_a_string_country(self) -> None:
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_search_page(groups=[]))

        _client(handler).search("whatsapp", country="  DEU  ")
        assert "gl=de" in seen["url"]

    def test_no_matching_apps_returns_empty(self) -> None:
        """A readable page with no results is empty, not a failure."""
        assert _serving(_search_page(groups=[])).search("nope") == []


class TestTheDetailVersionIsRead:
    """Play does publish a version for some apps, and it is in the detail payload.

    v0.6.0 replaced the ``[140][0][0][0]`` read with a flat constant on the claim
    that "Play publishes no current version anywhere". Verified live against the
    ``us`` storefront: Firefox reports ``153.0.1`` at that path, while Spotify and
    Duolingo have nothing there. So the field is per-app, and the old
    read-then-fall-back was right.
    """

    def test_a_published_version_is_returned(self) -> None:
        app = _serving(_detail_page(version="153.0.1")).lookup("com.whatsapp")

        assert app is not None
        assert app.version == "153.0.1"

    def test_an_absent_version_falls_back_to_the_placeholder(self) -> None:
        """Most apps genuinely have none; the placeholder is the honest answer."""
        app = _serving(_detail_page()).lookup("com.whatsapp")

        assert app is not None
        assert app.version == GooglePlaySearch.VERSION

    def test_a_malformed_version_node_does_not_raise(self) -> None:
        """The index is Google's, and it moves. A reshuffle must degrade, not crash."""
        app = _serving(_detail_page(version={"unexpected": "shape"})).lookup(
            "com.whatsapp"
        )

        assert app is not None
        assert app.version == GooglePlaySearch.VERSION

    def test_a_search_hit_still_has_no_version(self) -> None:
        """The compact search entry carries no version field at all."""
        results = _serving(_search_page(groups=[[_entry()]])).search("whatsapp")

        assert [a.version for a in results] == [GooglePlaySearch.VERSION]

    def test_deduplicates_by_app_id(self) -> None:
        page = _search_page(groups=[[_entry()], [_entry()]])
        assert len(_serving(page).search("whatsapp")) == 1

    def test_limit_is_applied(self) -> None:
        page = _search_page(groups=[[_entry(), _entry(app_id="com.whatsapp.w4b")]])
        assert len(_serving(page).search("whatsapp", limit=1)) == 1

    def test_zero_limit_returns_without_io(self) -> None:
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, text=_search_page(groups=[[_entry()]]))

        assert _client(handler).search("whatsapp", limit=0) == []
        assert calls == 0

    def test_negative_limit_is_rejected_without_io(self) -> None:
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, text="")

        with pytest.raises(ValueError, match="limit"):
            _client(handler).search("whatsapp", limit=-1)
        assert calls == 0

    def test_skips_an_entry_with_no_app_id(self) -> None:
        page = _search_page(groups=[[_entry(app_id=""), _entry(app_id="com.ok")]])
        results = _serving(page).search("whatsapp")
        assert [app.app_id for app in results] == ["com.ok"]

    def test_non_200_raises_http_error(self) -> None:
        with pytest.raises(HttpError, match="503"):
            _serving("", status=503).search("whatsapp")

    def test_public_search_403_is_not_an_auth_error(self) -> None:
        with pytest.raises(RequestError, match="403"):
            _serving("", status=403).search("whatsapp")

    def test_transport_failure_raises_http_error(self) -> None:
        def handler(request):
            raise httpx.ConnectError("refused")

        with pytest.raises(HttpError, match="refused"):
            _client(handler).search("whatsapp")


class TestSearchOnAnUnreadablePage:
    """An unreadable page is a scraper failure, never an empty result set.

    Google serves 200 for consent walls and throttling, and renumbers these
    datasets without notice. Returning ``[]`` would report all three as "no
    such app".
    """

    def test_no_af_callback_raises_parse_error(self) -> None:
        with pytest.raises(ParseError, match="no embedded data"):
            _serving("<html>no data</html>").search("whatsapp")

    def test_no_search_dataset_raises_parse_error(self) -> None:
        with pytest.raises(ParseError, match="no search results"):
            _serving(_page("ds:0", [1, 2, 3])).search("whatsapp")


class TestSearchTopResult:
    """The featured hit embeds a full detail block, so it is read as one."""

    def test_top_result_is_included(self) -> None:
        page = _search_page(groups=[], top=[None, None, _detail_block()])
        results = _serving(page).search("whatsapp")
        assert [app.app_id for app in results] == ["com.whatsapp"]

    def test_top_result_carries_a_rating_count(self) -> None:
        page = _search_page(groups=[], top=[None, None, _detail_block()])
        assert _serving(page).search("whatsapp")[0].rating_count == 232000

    def test_top_result_precedes_regular_entries(self) -> None:
        page = _search_page(
            groups=[[_entry(app_id="com.other")]],
            top=[None, None, _detail_block()],
        )
        results = _serving(page).search("whatsapp")
        assert [app.app_id for app in results] == ["com.whatsapp", "com.other"]

    def test_top_result_without_a_store_url_is_skipped(self) -> None:
        block = _detail_block()
        block[41] = None
        page = _search_page(groups=[[_entry()]], top=[None, None, block])
        results = _serving(page).search("whatsapp")
        assert [app.app_id for app in results] == ["com.whatsapp"]


class TestLookup:
    def test_maps_detail_fields(self) -> None:
        app = _serving(_detail_page()).lookup("com.whatsapp")
        assert app is not None
        assert app.app_id == "com.whatsapp"
        assert app.store == "googleplay"
        assert app.name == "WhatsApp Messenger"
        assert app.developer == "WhatsApp LLC"
        assert app.category == "Communication"
        assert app.rating == 4.7
        assert app.rating_count == 232000
        assert app.price == "Free"
        assert app.icon_url == _ICON

    def test_paid_price_is_formatted_from_micros(self) -> None:
        app = _serving(
            _detail_page(
                price_micros=1_990_000,
                formatted_price="$1.99",
            )
        ).lookup("com.whatsapp")
        assert app is not None
        assert app.price == "$1.99"

    def test_paid_price_preserves_the_storefront_currency(self) -> None:
        display = "TRY\N{NO-BREAK SPACE}109.00"
        app = _serving(
            _detail_page(
                price_micros=109_000_000,
                currency="TRY",
                formatted_price=display,
            )
        ).lookup("com.minecraft")

        assert app is not None
        assert app.price == display

    def test_search_result_preserves_the_storefront_currency(self) -> None:
        entry = _entry(
            price_micros=1_300_000_000,
            currency="JPY",
            formatted_price="¥1,300",
        )
        page = _search_page(groups=[[entry]])

        assert _serving(page).search("minecraft")[0].price == "¥1,300"

    @pytest.mark.parametrize(
        ("micros", "formatted"),
        [
            (-1, None),
            (-1_000_000, "$1.00"),
            (1_000_000, "-$1.00"),
            (1_000_000, "($1.00)"),
        ],
    )
    def test_negative_price_values_are_unknown(self, micros, formatted) -> None:
        app = _serving(
            _detail_page(price_micros=micros, formatted_price=formatted)
        ).lookup("com.whatsapp")

        assert app is not None
        assert app.price == "Unknown"

    def test_not_found_returns_none(self) -> None:
        assert _serving("Not Found", status=404).lookup("com.nope") is None

    def test_non_200_raises_http_error(self) -> None:
        with pytest.raises(HttpError, match="503"):
            _serving("", status=503).lookup("com.whatsapp")

    def test_transport_failure_raises_http_error(self) -> None:
        def handler(request):
            raise httpx.ConnectError("refused")

        with pytest.raises(HttpError, match="refused"):
            _client(handler).lookup("com.whatsapp")


class TestLookupOnAnUnreadablePage:
    """Never invent an app. A 404 means "no such app"; a 200 we cannot read
    means the scraper is broken, and the two must not look alike."""

    def test_no_af_callback_raises_parse_error(self) -> None:
        with pytest.raises(ParseError, match="no embedded data"):
            _serving("<html>no data</html>").lookup("com.whatsapp")

    def test_no_detail_dataset_raises_parse_error(self) -> None:
        with pytest.raises(ParseError, match="no app detail"):
            _serving(_page("ds:0", [1, 2, 3])).lookup("com.whatsapp")

    def test_a_nameless_detail_block_raises_parse_error(self) -> None:
        """ds:9 ships a stub at the detail path; a name is what confirms a hit."""
        page = _page("ds:9", [None, [None, None, [[None]]]])
        with pytest.raises(ParseError, match="no app detail"):
            _serving(page).lookup("com.whatsapp")


class TestUnusableScrapedValues:
    """Scraped values are untyped. A surprise must degrade, not crash."""

    def test_non_numeric_rating_falls_back_to_zero(self) -> None:
        app = _serving(_detail_page(rating="four-point-five")).lookup("com.whatsapp")
        assert app is not None
        assert app.rating == 0.0

    def test_non_numeric_rating_count_falls_back_to_zero(self) -> None:
        app = _serving(_detail_page(rating_count="lots")).lookup("com.whatsapp")
        assert app is not None
        assert app.rating_count == 0

    def test_non_finite_numbers_fall_back_without_aborting_lookup(self) -> None:
        app = _serving(_detail_page(rating="NaN", rating_count="Infinity")).lookup(
            "com.whatsapp"
        )

        assert app is not None
        assert app.rating == 0.0
        assert app.rating_count == 0

    @pytest.mark.parametrize(
        ("rating", "rating_count"),
        [(-0.1, 10), (5.1, 10), (True, 10), (4.5, -1), (4.5, 1.5), (4.5, True)],
    )
    def test_out_of_domain_numbers_fall_back_without_aborting_lookup(
        self, rating, rating_count
    ) -> None:
        app = _serving(_detail_page(rating=rating, rating_count=rating_count)).lookup(
            "com.whatsapp"
        )

        assert app is not None
        assert app.rating == (rating if rating == 4.5 else 0.0)
        assert app.rating_count == (rating_count if rating_count == 10 else 0)

    async def test_async_lookup_uses_the_same_domain_fallbacks(self) -> None:
        app = await _serving(_detail_page(rating=6, rating_count=-1)).alookup(
            "com.whatsapp"
        )

        assert app is not None
        assert app.rating == 0.0
        assert app.rating_count == 0

    def test_non_numeric_price_is_unknown(self) -> None:
        app = _serving(_detail_page(price_micros="free-ish")).lookup("com.whatsapp")
        assert app is not None
        assert app.price == "Unknown"

    @pytest.mark.parametrize(
        "price",
        [
            {},
            {0: 1_000_000, 1: "USD"},
            [],
            [1_000_000],
            [float("nan"), "USD", "$1.00"],
            [float("inf"), "USD", "$1.00"],
        ],
    )
    def test_lookup_malformed_price_container_is_unknown(self, price: Any) -> None:
        block = _detail_block()
        block[57] = [[[[[None, [price]]]]]]
        page = _page("ds:5", [None, [None, None, block]])

        app = _serving(page).lookup("com.whatsapp")

        assert app is not None
        assert app.price == "Unknown"

    @pytest.mark.parametrize(
        "price",
        [
            {},
            {0: 1_000_000, 1: "USD"},
            [],
            [1_000_000],
            [float("nan"), "USD", "$1.00"],
            [float("-inf"), "USD", "$1.00"],
        ],
    )
    def test_search_malformed_price_container_is_unknown(self, price: Any) -> None:
        entry = _entry()
        entry[0][8] = [None, [None, price]]
        page = _search_page(groups=[[entry]])

        assert _serving(page).search("whatsapp")[0].price == "Unknown"

    def test_a_non_string_name_falls_back(self) -> None:
        page = _search_page(groups=[[_entry(name=["WhatsApp"])]])
        assert _serving(page).search("whatsapp")[0].name == "Unknown"

    def test_a_non_string_rating_in_a_search_entry(self) -> None:
        page = _search_page(groups=[[_entry(rating={"stars": 5})]])
        assert _serving(page).search("whatsapp")[0].rating == 0.0


class TestFieldsPlayDoesNotPublish:
    def test_version_is_always_the_documented_placeholder(self) -> None:
        """Play publishes no current version anywhere on the page."""
        app = _serving(_detail_page()).lookup("com.whatsapp")
        assert app is not None
        assert app.version == "Varies with device"

    def test_a_regular_search_entry_has_no_rating_count(self) -> None:
        """The compact entry layout carries a rating but not a count."""
        assert _serving(_search_page()).search("whatsapp")[0].rating_count == 0


class TestAsyncParity:
    async def test_asearch_zero_limit_returns_without_io(self) -> None:
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, text=_search_page())

        assert await _client(handler).asearch("whatsapp", limit=0) == []
        assert calls == 0

    async def test_asearch_rejects_negative_limit_without_io(self) -> None:
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, text=_search_page())

        with pytest.raises(ValueError, match="limit"):
            await _client(handler).asearch("whatsapp", limit=-1)
        assert calls == 0

    async def test_asearch_matches_search(self) -> None:
        page = _search_page()
        sync = _serving(page).search("whatsapp")
        result = await _serving(page).asearch("whatsapp")
        assert [app.app_id for app in sync] == [app.app_id for app in result]

    async def test_alookup_matches_lookup(self) -> None:
        page = _detail_page()
        assert _serving(page).lookup("com.whatsapp") == await _serving(page).alookup(
            "com.whatsapp"
        )

    async def test_alookup_returns_none_when_absent(self) -> None:
        assert await _serving("Not Found", status=404).alookup("com.nope") is None

    async def test_asearch_raises_on_error_status(self) -> None:
        with pytest.raises(HttpError, match="503"):
            await _serving("", status=503).asearch("whatsapp")

    async def test_public_alookup_401_is_not_an_auth_error(self) -> None:
        with pytest.raises(RequestError, match="401"):
            await _serving("", status=401).alookup("com.whatsapp")

    async def test_alookup_raises_on_error_status(self) -> None:
        with pytest.raises(HttpError, match="500"):
            await _serving("", status=500).alookup("com.whatsapp")

    async def test_asearch_raises_on_transport_failure(self) -> None:
        def handler(request):
            raise httpx.ConnectError("refused")

        with pytest.raises(HttpError, match="refused"):
            await _client(handler).asearch("whatsapp")

    async def test_alookup_raises_parse_error_on_an_unreadable_page(self) -> None:
        with pytest.raises(ParseError, match="no embedded data"):
            await _serving("<html>no data</html>").alookup("com.whatsapp")


class TestAnUnrelatedDatasetIsNotAnEmptyResultSet:
    """A list at the sections path is not proof the page is a search results
    page, since any dataset can have one. Treating it as readable turned "we could
    not read this page" back into "this query has no results"."""

    def test_a_page_with_only_an_unrelated_dataset_raises(self):
        page = _page("ds:3", [["banner", ["a", "b"]]])

        with pytest.raises(ParseError, match="no search results"):
            _serving(page).search("whatsapp")

    def test_a_real_but_empty_results_section_is_still_empty(self):
        assert _serving(_search_page(groups=[])).search("nope") == []


class TestReleaseDates:
    """Play renders both dates on the detail page, as display strings.

    Verified live against the ``us`` storefront: ``[10][0]`` holds "Released on"
    and ``[145][0][0]`` holds "Updated on". Firefox reported
    ``Dec 21, 2010`` and ``Jul 30, 2026``.

    They are formatted for a reader, not an API: day precision only, month name in
    the request's ``hl`` language. This client pins ``hl=en``, which is what makes
    parsing them viable at all.
    """

    def test_both_dates_are_mapped(self):
        app = _serving(
            _detail_page(released_on="Dec 21, 2010", updated_on="Jul 30, 2026")
        ).lookup("com.whatsapp")

        assert app is not None
        assert app.first_release_date == datetime(2010, 12, 21, tzinfo=UTC)
        assert app.current_version_release_date == datetime(2026, 7, 30, tzinfo=UTC)

    def test_dates_are_utc_midnight_because_play_gives_no_time(self):
        """Day precision is all Play publishes. Midnight UTC is the stated
        convention, not a real timestamp; see ``AppMetadata``."""
        app = _serving(_detail_page(updated_on="Jul 30, 2026")).lookup("com.whatsapp")

        assert app is not None
        assert app.current_version_release_date.hour == 0
        assert app.current_version_release_date.tzinfo is not None

    def test_absent_dates_are_none(self):
        app = _serving(_detail_page()).lookup("com.whatsapp")

        assert app is not None
        assert app.first_release_date is None
        assert app.current_version_release_date is None

    @pytest.mark.parametrize(
        "bad",
        ["", "yesterday", "30 Jul 2026", "2026-07-30", "Jul 30 2026", None, 0, []],
    )
    def test_an_unparseable_display_date_costs_the_field_only(self, bad):
        """These indices are Google's and they move. A reshuffle must degrade."""
        app = _serving(_detail_page(released_on=bad, updated_on=bad)).lookup(
            "com.whatsapp"
        )

        assert app is not None
        assert app.name == "WhatsApp Messenger"
        assert app.first_release_date is None

    def test_a_search_hit_has_no_dates(self):
        """The compact entry carries neither, so both stay None."""
        results = _serving(_search_page(groups=[[_entry()]])).search("whatsapp")

        assert [a.first_release_date for a in results] == [None]
        assert [a.current_version_release_date for a in results] == [None]

    def test_the_featured_hit_gets_them(self):
        """A featured hit embeds a full detail block, so it is read the same way."""
        page = _search_page(
            groups=[],
            top=[
                None,
                None,
                _detail_block(released_on="Dec 21, 2010", updated_on="Jul 30, 2026"),
            ],
        )

        results = _serving(page).search("whatsapp")

        assert results[0].first_release_date == datetime(2010, 12, 21, tzinfo=UTC)
