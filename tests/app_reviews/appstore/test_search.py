"""Tests for AppStoreSearch."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from app_reviews.appstore.search import AppStoreSearch
from app_reviews.core.http import HttpClient
from app_reviews.errors import AppReviewsError, HttpError, ParseError, RequestError
from app_reviews.models.config import RetryConfig
from app_reviews.models.country import Country


def _itunes_result(
    track_id=310633997, name="WhatsApp", bundle_id="com.whatsapp.WhatsApp"
):
    return {
        "trackId": track_id,
        "bundleId": bundle_id,
        "trackName": name,
        "artistName": "WhatsApp Inc.",
        "primaryGenreName": "Social Networking",
        "formattedPrice": "Free",
        "version": "24.10.79",
        "averageUserRating": 4.7,
        "userRatingCount": 5000000,
        "trackViewUrl": f"https://apps.apple.com/app/id{track_id}",
        "artworkUrl512": "https://is1-ssl.mzstatic.com/image/512x512.jpg",
    }


def _payload(results):
    return json.dumps({"resultCount": len(results), "results": results})


def _client(handler):
    # max_retries=0: these tests assert on the first failure, not on retry
    # behaviour (already covered by tests/app_reviews/http/test_client.py);
    # the default RetryConfig would otherwise sleep through real backoff
    # delays on every error-status/transport-failure test.
    return AppStoreSearch(
        http=HttpClient(
            transport=httpx.MockTransport(handler),
            retry=RetryConfig(max_retries=0),
        )
    )


class TestConstruction:
    def test_default(self):
        client = AppStoreSearch()
        assert client is not None

    def test_with_proxy(self):
        client = AppStoreSearch(proxy="http://proxy:8080")
        assert client._http._proxy == "http://proxy:8080"


class TestSearch:
    def test_zero_limit_returns_without_io(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, text=_payload([_itunes_result()]))

        assert _client(handler).search("whatsapp", limit=0) == []
        assert calls == 0

    def test_negative_limit_is_rejected_without_io(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, text=_payload([]))

        with pytest.raises(ValueError, match="limit"):
            _client(handler).search("whatsapp", limit=-1)
        assert calls == 0

    def test_maps_results(self):
        def handler(request):
            return httpx.Response(200, text=_payload([_itunes_result()]))

        results = _client(handler).search("whatsapp")

        assert len(results) == 1
        app = results[0]
        assert app.app_id == "310633997"
        assert app.store == "appstore"
        assert app.name == "WhatsApp"
        assert app.developer == "WhatsApp Inc."
        assert app.category == "Social Networking"
        assert app.price == "Free"
        assert app.version == "24.10.79"
        assert app.rating == 4.7
        assert app.rating_count == 5000000
        assert app.icon_url == "https://is1-ssl.mzstatic.com/image/512x512.jpg"

    def test_sends_query_params(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_payload([]))

        _client(handler).search("whatsapp", country=Country.GB, limit=5)

        assert "term=whatsapp" in seen["url"]
        assert "country=gb" in seen["url"]
        assert "limit=5" in seen["url"]
        assert "entity=software" in seen["url"]

    def test_accepts_a_plain_string_country(self):
        """The enum is a convenience, not the only accepted form."""
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_payload([]))

        _client(handler).search("whatsapp", country="jp", limit=5)

        assert "country=jp" in seen["url"]

    def test_normalises_a_string_country(self):
        """Case, whitespace and alpha-3 all resolve to the alpha-2 form."""
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_payload([]))

        _client(handler).search("whatsapp", country="  GBR  ", limit=5)

        assert "country=gb" in seen["url"]

    def test_empty_results_returns_empty_list(self):
        def handler(request):
            return httpx.Response(200, text=_payload([]))

        assert _client(handler).search("xyznonexistent") == []

    def test_non_finite_metadata_numbers_fall_back_without_aborting_search(self):
        result = _itunes_result()
        result["averageUserRating"] = "NaN"
        result["userRatingCount"] = "Infinity"

        apps = _client(
            lambda request: httpx.Response(200, text=_payload([result]))
        ).search("whatsapp")

        assert apps[0].rating == 0.0
        assert apps[0].rating_count == 0

    def test_huge_metadata_numbers_fall_back_without_aborting_search(self):
        result = _itunes_result()
        result["averageUserRating"] = 10**400
        result["userRatingCount"] = 10**400

        [app] = _client(
            lambda request: httpx.Response(200, text=_payload([result]))
        ).search("whatsapp")

        assert app.rating == 0.0
        assert app.rating_count == 0

    @pytest.mark.parametrize(
        ("rating", "rating_count"),
        [(-0.1, 10), (5.1, 10), (True, 10), (4.5, -1), (4.5, 1.5), (4.5, True)],
    )
    def test_out_of_domain_metadata_numbers_fall_back(self, rating, rating_count):
        result = _itunes_result()
        result["averageUserRating"] = rating
        result["userRatingCount"] = rating_count

        [app] = _client(
            lambda request: httpx.Response(200, text=_payload([result]))
        ).search("whatsapp")

        assert app.rating == (rating if rating == 4.5 else 0.0)
        assert app.rating_count == (rating_count if rating_count == 10 else 0)

    def test_non_200_raises_http_error(self):
        def handler(request):
            return httpx.Response(503, text="")

        with pytest.raises(HttpError, match="503"):
            _client(handler).search("whatsapp")

    def test_public_search_403_is_not_an_auth_error(self):
        with pytest.raises(RequestError, match="403"):
            _client(lambda request: httpx.Response(403)).search("whatsapp")

    def test_malformed_json_raises_rather_than_looking_empty(self):
        """An unreadable body is not "no results"; see
        ``TestMalformedResponsesAreParseErrors`` for the full contract."""

        def handler(request):
            return httpx.Response(200, text="not json")

        with pytest.raises(ParseError):
            _client(handler).search("whatsapp")

    def test_transport_failure_raises_http_error(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        with pytest.raises(HttpError, match="refused"):
            _client(handler).search("whatsapp")


class TestLookup:
    def test_numeric_id_uses_the_id_param(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_payload([_itunes_result()]))

        _client(handler).lookup("310633997")

        assert "id=310633997" in seen["url"]
        assert "bundleId=" not in seen["url"]

    def test_reverse_dns_id_uses_the_bundle_param(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_payload([_itunes_result()]))

        _client(handler).lookup("com.whatsapp.WhatsApp")

        assert "bundleId=com.whatsapp.WhatsApp" in seen["url"]

    def test_passes_country_param(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=_payload([_itunes_result()]))

        _client(handler).lookup("com.whatsapp.WhatsApp", country=Country.JP)

        assert "country=jp" in seen["url"]

    def test_maps_result(self):
        def handler(request):
            return httpx.Response(200, text=_payload([_itunes_result()]))

        result = _client(handler).lookup("310633997")

        assert result.name == "WhatsApp"
        assert result.icon_url == "https://is1-ssl.mzstatic.com/image/512x512.jpg"

    def test_no_results_returns_none(self):
        def handler(request):
            return httpx.Response(200, text=_payload([]))

        assert _client(handler).lookup("99999") is None

    def test_malformed_json_raises_rather_than_looking_absent(self):
        """An unreadable body is not "no such app"."""

        def handler(request):
            return httpx.Response(200, text="not json")

        with pytest.raises(ParseError):
            _client(handler).lookup("99999")

    def test_non_200_raises_http_error(self):
        def handler(request):
            return httpx.Response(404, text="")

        with pytest.raises(HttpError, match="404"):
            _client(handler).lookup("310633997")


class TestAsyncParity:
    async def test_asearch_matches_search(self):
        def handler(request):
            return httpx.Response(200, text=_payload([_itunes_result()]))

        sync_results = _client(handler).search("whatsapp")
        async_results = await _client(handler).asearch("whatsapp")

        assert [r.app_id for r in sync_results] == [r.app_id for r in async_results]

    async def test_alookup_matches_lookup(self):
        def handler(request):
            return httpx.Response(200, text=_payload([_itunes_result()]))

        sync_result = _client(handler).lookup("310633997")
        async_result = await _client(handler).alookup("310633997")

        assert sync_result == async_result

    async def test_alookup_falls_back_for_invalid_metadata_domains(self):
        result = _itunes_result()
        result["averageUserRating"] = 6
        result["userRatingCount"] = -1

        app = await _client(
            lambda request: httpx.Response(200, text=_payload([result]))
        ).alookup("310633997")

        assert app is not None
        assert app.rating == 0.0
        assert app.rating_count == 0

    async def test_alookup_returns_none_when_absent(self):
        def handler(request):
            return httpx.Response(200, text=_payload([]))

        assert await _client(handler).alookup("310633997") is None

    async def test_asearch_raises_on_error_status(self):
        def handler(request):
            return httpx.Response(503, text="")

        with pytest.raises(HttpError, match="503"):
            await _client(handler).asearch("whatsapp")

    async def test_alookup_raises_on_error_status(self):
        def handler(request):
            return httpx.Response(500, text="")

        with pytest.raises(HttpError, match="500"):
            await _client(handler).alookup("310633997")

    async def test_asearch_raises_on_transport_failure(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        with pytest.raises(HttpError, match="refused"):
            await _client(handler).asearch("whatsapp")


class TestMalformedResponsesAreParseErrors:
    """A garbage body is not "no results".

    ``search``/``lookup`` raise rather than returning data, so a body we cannot
    read must arrive as ``ParseError``, or it is indistinguishable from an
    app that genuinely does not exist.
    """

    def _client(self, body: str, status: int = 200):
        return AppStoreSearch(
            http=HttpClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(status, text=body)
                )
            )
        )

    @pytest.mark.parametrize("body", ["not json", "", "<html>nope</html>"])
    def test_search_raises_on_unreadable_json(self, body):
        with pytest.raises(ParseError):
            self._client(body).search("x")

    @pytest.mark.parametrize("body", ["not json", "", "<html>nope</html>"])
    def test_lookup_raises_on_unreadable_json(self, body):
        with pytest.raises(ParseError):
            self._client(body).lookup("123")

    @pytest.mark.parametrize("results", ['{"a": 1}', '"text"', "5"])
    def test_a_non_list_results_is_a_parse_error(self, results):
        with pytest.raises(ParseError):
            self._client(f'{{"results": {results}}}').search("x")

    def test_a_parse_error_is_catchable_as_the_base(self):
        with pytest.raises(AppReviewsError):
            self._client("not json").search("x")

    def test_an_empty_result_set_is_still_empty_not_an_error(self):
        assert self._client('{"results": []}').search("x") == []
        assert self._client('{"results": []}').lookup("123") is None

    async def test_the_async_paths_raise_too(self):
        with pytest.raises(ParseError):
            await self._client("not json").asearch("x")
        with pytest.raises(ParseError):
            await self._client("not json").alookup("123")


class TestNullsDoNotBecomeValues:
    """``dict.get(k, default)`` returns the default only when the key is *absent*.

    A key present with ``null`` returns ``None``, which was landing in fields
    declared ``str`` and ``float``.
    """

    def _one(self, result: dict):
        client = AppStoreSearch(
            http=HttpClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(
                        200, text=json.dumps({"results": [result]})
                    )
                )
            )
        )
        return client.search("x")

    def test_a_null_name_does_not_become_none(self):
        [app] = self._one({"trackId": 1, "trackName": None})

        assert isinstance(app.name, str)

    def test_a_null_rating_does_not_become_none(self):
        [app] = self._one({"trackId": 1, "averageUserRating": None})

        assert isinstance(app.rating, float)

    def test_a_null_rating_count_does_not_become_none(self):
        [app] = self._one({"trackId": 1, "userRatingCount": None})

        assert isinstance(app.rating_count, int)

    def test_a_non_numeric_rating_is_not_propagated(self):
        [app] = self._one({"trackId": 1, "averageUserRating": "four"})

        assert isinstance(app.rating, float)

    def test_every_declared_type_holds_on_a_sparse_result(self):
        import dataclasses

        [app] = self._one({"trackId": 1})
        types = {
            "app_id": str,
            "name": str,
            "developer": str,
            "category": str,
            "price": str,
            "version": str,
            "rating": float,
            "rating_count": int,
            "url": str,
        }
        for f in dataclasses.fields(app):
            if f.name in types:
                assert isinstance(getattr(app, f.name), types[f.name]), f.name


class TestResultsWithoutAnIdAreSkipped:
    """``str(None)`` produced the literal app id ``"None"``."""

    def _search(self, results: list[dict]):
        client = AppStoreSearch(
            http=HttpClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, text=json.dumps({"results": results}))
                )
            )
        )
        return client.search("x")

    @pytest.mark.parametrize(
        "result",
        [{"trackName": "App"}, {"trackId": None, "trackName": "App"}, {"trackId": ""}],
    )
    def test_a_result_without_a_usable_id_is_dropped(self, result):
        assert self._search([result]) == []

    def test_the_usable_results_survive(self):
        apps = self._search(
            [{"trackId": 1, "trackName": "A"}, {"trackName": "B"}, {"trackId": 3}]
        )

        assert [a.app_id for a in apps] == ["1", "3"]

    def test_a_bundle_id_still_works_when_there_is_no_track_id(self):
        [app] = self._search([{"bundleId": "com.example.app", "trackName": "A"}])

        assert app.app_id == "com.example.app"

    def test_lookup_returns_none_rather_than_raising(self):
        """``lookup`` already signals "no usable app" with None."""
        client = AppStoreSearch(
            http=HttpClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(
                        200, text=json.dumps({"results": [{"trackName": "A"}]})
                    )
                )
            )
        )

        assert client.lookup("123") is None


class TestTheAsyncPathSendsTheSameQuery:
    """``aget_and_parse`` dropping ``params`` entirely passed the suite: every
    async assertion compared parsed output from a handler that ignored the
    request, so ``asearch`` sending no ``term`` at all looked identical."""

    async def test_asearch_sends_the_query_parameters(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=json.dumps({"results": []}))

        await _client(handler).asearch("whatsapp", country=Country.DE, limit=7)

        assert "term=whatsapp" in seen["url"]
        assert "country=de" in seen["url"]
        assert "limit=7" in seen["url"]

    async def test_asearch_zero_limit_returns_without_io(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, text=_payload([_itunes_result()]))

        assert await _client(handler).asearch("whatsapp", limit=0) == []
        assert calls == 0

    async def test_asearch_rejects_negative_limit_without_io(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, text=_payload([]))

        with pytest.raises(ValueError, match="limit"):
            await _client(handler).asearch("whatsapp", limit=-1)
        assert calls == 0

    async def test_public_asearch_401_is_not_an_auth_error(self):
        with pytest.raises(RequestError, match="401"):
            await _client(lambda request: httpx.Response(401)).asearch("whatsapp")

    async def test_alookup_sends_the_identifier(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text=json.dumps({"results": []}))

        await _client(handler).alookup("389801252", country=Country.GB)

        assert "id=389801252" in seen["url"]
        assert "country=gb" in seen["url"]


class TestAnUnusableNameFallsBackRatherThanStringifying:
    """``isinstance(app.name, str)`` is satisfied by the literal ``"None"``,
    which is exactly the bug ``scraped_text`` exists to prevent."""

    def test_a_null_name_becomes_the_placeholder(self):
        body = json.dumps({"results": [{"trackId": 1, "trackName": None}]})
        app = _client(lambda r: httpx.Response(200, text=body)).lookup("1")

        assert app.name == "Unknown"

    def test_a_missing_name_becomes_the_placeholder(self):
        body = json.dumps({"results": [{"trackId": 1}]})
        app = _client(lambda r: httpx.Response(200, text=body)).lookup("1")

        assert app.name == "Unknown"


class TestReleaseDates:
    """iTunes already sends both dates in the response this client parses.

    ``currentVersionReleaseDate`` and ``releaseDate`` are in the same result dict
    as ``version`` and ``trackName``; mapping them costs no extra request.
    """

    def _lookup(self, **extra):
        result = {
            "trackId": 310633997,
            "trackName": "WhatsApp Messenger",
            **extra,
        }
        return _client(lambda _r: httpx.Response(200, text=_payload([result]))).lookup(
            "310633997"
        )

    def test_both_dates_are_mapped(self):
        app = self._lookup(
            currentVersionReleaseDate="2026-08-01T03:51:32Z",
            releaseDate="2009-05-04T02:43:49Z",
        )

        assert app is not None
        assert app.current_version_release_date == datetime(
            2026, 8, 1, 3, 51, 32, tzinfo=UTC
        )
        assert app.first_release_date == datetime(2009, 5, 4, 2, 43, 49, tzinfo=UTC)

    def test_they_are_timezone_aware(self):
        """A naive datetime silently compares wrong against an aware one."""
        app = self._lookup(currentVersionReleaseDate="2026-08-01T03:51:32Z")

        assert app is not None
        assert app.current_version_release_date.tzinfo is not None

    def test_absent_dates_are_none_not_a_placeholder(self):
        """There is no honest placeholder date. A sentinel would sort and filter as
        if it were real, which is the bug class that dated Play reviews to 1973."""
        app = self._lookup()

        assert app is not None
        assert app.current_version_release_date is None
        assert app.first_release_date is None

    @pytest.mark.parametrize("bad", ["", "not-a-date", None, [], 0, True])
    def test_an_unusable_value_costs_the_field_not_the_result(self, bad):
        app = self._lookup(currentVersionReleaseDate=bad, releaseDate=bad)

        assert app is not None
        assert app.name == "WhatsApp Messenger"
        assert app.current_version_release_date is None

    def test_search_results_carry_them_too(self):
        """Search and lookup share one mapper, so both get the dates."""
        results = _client(
            lambda _r: httpx.Response(
                200,
                text=_payload(
                    [
                        {
                            "trackId": 1,
                            "trackName": "A",
                            "releaseDate": "2020-01-02T00:00:00Z",
                        }
                    ]
                ),
            )
        ).search("a")

        assert results[0].first_release_date == datetime(2020, 1, 2, tzinfo=UTC)
