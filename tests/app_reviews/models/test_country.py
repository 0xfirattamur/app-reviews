"""Tests for Country enum and region groups."""

import pytest

from app_reviews.models.country import Country, normalise_country


def test_all_group_contains_every_member():
    assert frozenset(Country) == Country.ALL


def test_europe_group():
    assert Country.DE in Country.EUROPE
    assert Country.FR in Country.EUROPE
    assert Country.GB in Country.EUROPE
    assert Country.US not in Country.EUROPE


def test_americas_group():
    assert {country.value for country in Country.AMERICAS} == {
        "ag",
        "ai",
        "ar",
        "bb",
        "bm",
        "bo",
        "br",
        "bs",
        "bz",
        "ca",
        "cl",
        "co",
        "cr",
        "dm",
        "do",
        "ec",
        "gd",
        "gt",
        "gy",
        "hn",
        "jm",
        "kn",
        "ky",
        "lc",
        "mx",
        "ni",
        "pa",
        "pe",
        "py",
        "sr",
        "sv",
        "tc",
        "tt",
        "us",
        "uy",
        "vc",
        "ve",
        "vg",
    }


def test_asia_pacific_group():
    assert Country.JP in Country.ASIA_PACIFIC
    assert Country.KR in Country.ASIA_PACIFIC
    assert Country.AU in Country.ASIA_PACIFIC
    assert Country.US not in Country.ASIA_PACIFIC


def test_middle_east_group():
    assert Country.SA in Country.MIDDLE_EAST
    assert Country.AE in Country.MIDDLE_EAST
    assert Country.QA in Country.MIDDLE_EAST
    assert Country.US not in Country.MIDDLE_EAST


def test_english_speaking_group():
    assert Country.US in Country.ENGLISH_SPEAKING
    assert Country.GB in Country.ENGLISH_SPEAKING
    assert Country.AU in Country.ENGLISH_SPEAKING
    assert Country.CA in Country.ENGLISH_SPEAKING
    assert Country.DE not in Country.ENGLISH_SPEAKING


def test_string_accepted_where_country_expected():
    countries = [Country.US, Country.GB]
    assert "us" in countries
    assert "gb" in countries


class TestNormaliseCountry:
    def test_every_storefront_has_an_alpha3(self):
        from app_reviews.models.country import _ALPHA3_TO_ALPHA2

        assert set(_ALPHA3_TO_ALPHA2.values()) == {c.value for c in Country}

    def test_the_mapping_is_one_to_one(self):
        from app_reviews.models.country import _ALPHA3_TO_ALPHA2

        assert len(set(_ALPHA3_TO_ALPHA2)) == len(set(_ALPHA3_TO_ALPHA2.values()))

    @pytest.mark.parametrize(
        ("alpha3", "alpha2"),
        [
            # The pairs where a wrong entry would be a silent data bug.
            ("CHN", "cn"),
            ("CHE", "ch"),
            ("SVN", "si"),
            ("SVK", "sk"),
            ("AUT", "at"),
            ("AUS", "au"),
            ("IRL", "ie"),
            ("ISL", "is"),
            ("ISR", "il"),
            ("NER", "ne"),
            ("NGA", "ng"),
            ("MLI", "ml"),
            ("MLT", "mt"),
            ("LBR", "lr"),
            ("LBN", "lb"),
            ("ARE", "ae"),
            ("ARG", "ar"),
            ("ARM", "am"),
            ("BHR", "bh"),
            ("BHS", "bs"),
            ("SLV", "sv"),
            ("SLE", "sl"),
            ("SLB", "sb"),
            ("CYM", "ky"),
            ("CYP", "cy"),
            ("MAC", "mo"),
            ("MDA", "md"),
            ("MDG", "mg"),
        ],
    )
    def test_confusable_codes_map_correctly(self, alpha3, alpha2):
        assert normalise_country(alpha3) == alpha2

    def test_alpha2_passes_through_lowercased(self):
        assert normalise_country("GB") == "gb"
        assert normalise_country("us") == "us"

    @pytest.mark.parametrize("empty", [None, ""])
    def test_absent_stays_absent(self, empty):
        assert normalise_country(empty) is None

    def test_an_unknown_code_is_kept_and_logged(self, caplog):
        """Losing a storefront is worse than reporting an unfamiliar one."""
        import logging

        with caplog.at_level(logging.WARNING):
            assert normalise_country("XYZ") == "XYZ"

        assert "Unrecognised storefront" in caplog.text

    def test_an_unknown_two_letter_code_is_also_logged(self, caplog):
        """The docstring promises unrecognised codes are logged so they can be
        added here. That has to hold for both alphabets, not just alpha-3."""
        import logging

        with caplog.at_level(logging.WARNING):
            assert normalise_country("xx") == "xx"

        assert "Unrecognised storefront" in caplog.text

    def test_a_known_two_letter_code_is_not_logged(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            assert normalise_country("DE") == "de"

        assert caplog.text == ""

    def test_a_known_alpha3_is_not_logged(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            assert normalise_country("DEU") == "de"

        assert caplog.text == ""
