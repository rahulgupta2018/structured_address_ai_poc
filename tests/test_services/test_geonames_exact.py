"""Tests for geonames_exact service (Step 3) — disambiguation logic."""

from __future__ import annotations

from unittest.mock import patch

from services.geonames_exact import _disambiguate, match

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with your own data               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Ambiguous city name (exists in multiple US states)
SAMPLE_AMBIGUOUS_CITY = "Springfield"
SAMPLE_AMBIGUOUS_COUNTRY = "US"

# Unique city (single match)
SAMPLE_UNIQUE_CITY = "San Francisco"
SAMPLE_UNIQUE_COUNTRY = "US"
SAMPLE_UNIQUE_GEONAMEID = 5391959
SAMPLE_UNIQUE_ADMIN1 = "CA"
SAMPLE_UNIQUE_REGION = "California"
SAMPLE_UNIQUE_POPULATION = 864816

# Alternate name test (input spelling differs from GeoNames primary)
SAMPLE_ALTERNATE_INPUT = "Morbi"       # input spelling (alternate name)
SAMPLE_ALTERNATE_PRIMARY = "Morvi"     # GeoNames primary name
SAMPLE_ALTERNATE_COUNTRY = "IN"
SAMPLE_ALTERNATE_GEONAMEID = 1262775

# Non-existent city
SAMPLE_NONEXISTENT_CITY = "Xyzzyville"


# ── Fixtures ─────────────────────────────────────────────────────────────────

SPRINGFIELD_MO = {
    "geonameid": 4409896, "name": SAMPLE_AMBIGUOUS_CITY, "ascii_name": SAMPLE_AMBIGUOUS_CITY,
    "country_code": SAMPLE_AMBIGUOUS_COUNTRY, "admin1_code": "MO", "population": 170188,
    "name_type": "primary",
}
SPRINGFIELD_MA = {
    "geonameid": 4951788, "name": SAMPLE_AMBIGUOUS_CITY, "ascii_name": SAMPLE_AMBIGUOUS_CITY,
    "country_code": SAMPLE_AMBIGUOUS_COUNTRY, "admin1_code": "MA", "population": 154341,
    "name_type": "primary",
}
SPRINGFIELD_IL = {
    "geonameid": 4250542, "name": SAMPLE_AMBIGUOUS_CITY, "ascii_name": SAMPLE_AMBIGUOUS_CITY,
    "country_code": SAMPLE_AMBIGUOUS_COUNTRY, "admin1_code": "IL", "population": 114394,
    "name_type": "primary",
}
SPRINGFIELD_OR = {
    "geonameid": 5754005, "name": SAMPLE_AMBIGUOUS_CITY, "ascii_name": SAMPLE_AMBIGUOUS_CITY,
    "country_code": SAMPLE_AMBIGUOUS_COUNTRY, "admin1_code": "OR", "population": 60870,
    "name_type": "primary",
}

ALL_SPRINGFIELDS = [SPRINGFIELD_MO, SPRINGFIELD_MA, SPRINGFIELD_IL, SPRINGFIELD_OR]


# ── _disambiguate() unit tests ───────────────────────────────────────────────


class TestDisambiguate:
    """Test the core _disambiguate function."""

    def test_admin1_code_selects_correct_city(self):
        """With postal admin1=IL, should pick Springfield IL, not MO."""
        result = _disambiguate(ALL_SPRINGFIELDS, postal_admin1_code="IL", postal_region="Illinois")
        report("disambiguate (admin1=IL)", {
            "candidates": [f"{c['name']}({c['admin1_code']}, pop={c['population']})" for c in ALL_SPRINGFIELDS],
            "postal_admin1_code": "IL",
            "winner": f"{result['name']}({result['admin1_code']})",
        })
        assert result["geonameid"] == SPRINGFIELD_IL["geonameid"]
        assert result["admin1_code"] == "IL"

    def test_admin1_code_ma(self):
        """With postal admin1=MA, should pick Springfield MA."""
        result = _disambiguate(ALL_SPRINGFIELDS, postal_admin1_code="MA", postal_region="Massachusetts")
        assert result["geonameid"] == SPRINGFIELD_MA["geonameid"]

    def test_no_postal_signal_falls_back_to_population(self):
        """Without postal signals, picks the largest city (MO)."""
        result = _disambiguate(ALL_SPRINGFIELDS, postal_admin1_code=None, postal_region=None)
        report("disambiguate (no postal signal)", {
            "strategy": "population fallback",
            "winner": f"{result['name']}({result['admin1_code']}, pop={result['population']})",
        })
        assert result["geonameid"] == SPRINGFIELD_MO["geonameid"]

    def test_unknown_admin1_falls_back_to_population(self):
        """If postal admin1 doesn't match any city, falls back to population."""
        result = _disambiguate(ALL_SPRINGFIELDS, postal_admin1_code="XX", postal_region=None)
        assert result["geonameid"] == SPRINGFIELD_MO["geonameid"]

    def test_single_match_returns_it(self):
        """With only one match, no disambiguation needed."""
        result = _disambiguate([SPRINGFIELD_IL], postal_admin1_code=None, postal_region=None)
        assert result["geonameid"] == SPRINGFIELD_IL["geonameid"]

    def test_empty_matches_returns_empty(self):
        result = _disambiguate([], postal_admin1_code="IL", postal_region=None)
        assert result == {}

    def test_case_insensitive_admin1(self):
        """Admin1 matching should be case-insensitive."""
        result = _disambiguate(ALL_SPRINGFIELDS, postal_admin1_code="il", postal_region=None)
        assert result["geonameid"] == SPRINGFIELD_IL["geonameid"]


# ── match() integration tests (with mocked repo) ────────────────────────────


class TestMatchWithDisambiguation:
    """Test the full match() function with disambiguation signals."""

    @patch("services.geonames_exact.resolve_all_cities_by_name")
    def test_springfield_il_with_postal_signal(self, mock_resolve_all):
        """Ambiguous city + postal admin1=IL → picks IL, not MO."""
        mock_resolve_all.return_value = ALL_SPRINGFIELDS

        state = {
            "libpostal_town": SAMPLE_AMBIGUOUS_CITY,
            "postal_town_candidate": None,
            "postal_admin1_code": "IL",
            "postal_region": "Illinois",
            "country_code": SAMPLE_AMBIGUOUS_COUNTRY,
        }
        report("match input", state)
        result = match(state)
        report("match output", {
            "exact_match": result["exact_match"],
            "geonames_id": result["geonames_id"],
            "town_candidate": result["town_candidate"],
            "match_type": result["match_type"],
        })
        assert result["exact_match"] is True
        assert result["geonames_id"] == SPRINGFIELD_IL["geonameid"]
        assert result["town_candidate"] == SAMPLE_AMBIGUOUS_CITY
        assert result["match_type"] == "primary"

    @patch("services.geonames_exact.resolve_all_cities_by_name")
    def test_springfield_no_postal_signal_picks_largest(self, mock_resolve_all):
        """Ambiguous city without postal signal → picks largest population."""
        mock_resolve_all.return_value = ALL_SPRINGFIELDS

        state = {
            "libpostal_town": SAMPLE_AMBIGUOUS_CITY,
            "postal_town_candidate": None,
            "postal_admin1_code": None,
            "postal_region": None,
            "country_code": SAMPLE_AMBIGUOUS_COUNTRY,
        }
        report("match input (no postal)", state)
        result = match(state)
        report("match output (no postal)", {
            "exact_match": result["exact_match"],
            "geonames_id": result["geonames_id"],
        })
        assert result["exact_match"] is True
        assert result["geonames_id"] == SPRINGFIELD_MO["geonameid"]

    @patch("services.geonames_exact.resolve_all_cities_by_name")
    def test_unique_city_no_disambiguation_needed(self, mock_resolve_all):
        """Single match — no disambiguation logic triggered."""
        mock_resolve_all.return_value = [
            {
                "geonameid": SAMPLE_UNIQUE_GEONAMEID,
                "name": SAMPLE_UNIQUE_CITY,
                "ascii_name": SAMPLE_UNIQUE_CITY,
                "country_code": SAMPLE_UNIQUE_COUNTRY,
                "admin1_code": SAMPLE_UNIQUE_ADMIN1,
                "population": SAMPLE_UNIQUE_POPULATION,
                "name_type": "primary",
            }
        ]

        state = {
            "libpostal_town": SAMPLE_UNIQUE_CITY,
            "postal_town_candidate": None,
            "postal_admin1_code": SAMPLE_UNIQUE_ADMIN1,
            "postal_region": SAMPLE_UNIQUE_REGION,
            "country_code": SAMPLE_UNIQUE_COUNTRY,
        }
        report("match input (unique)", state)
        result = match(state)
        report("match output (unique)", {
            "exact_match": result["exact_match"],
            "geonames_id": result["geonames_id"],
        })
        assert result["exact_match"] is True
        assert result["geonames_id"] == SAMPLE_UNIQUE_GEONAMEID

    @patch("services.geonames_exact.resolve_all_cities_by_name", return_value=[])
    def test_no_match_returns_unmatched(self, mock_resolve_all):
        state = {
            "libpostal_town": SAMPLE_NONEXISTENT_CITY,
            "postal_town_candidate": None,
            "postal_admin1_code": None,
            "postal_region": None,
            "country_code": SAMPLE_AMBIGUOUS_COUNTRY,
        }
        report("match input (nonexistent)", state)
        result = match(state)
        report("match output (nonexistent)", {
            "exact_match": result["exact_match"],
            "geonames_id": result["geonames_id"],
        })
        assert result["exact_match"] is False
        assert result["geonames_id"] is None

    @patch("services.geonames_exact.resolve_all_cities_by_name")
    def test_alternate_name_preserves_input_spelling(self, mock_resolve_all):
        """Alternate name should preserve input spelling."""
        mock_resolve_all.return_value = [
            {
                "geonameid": SAMPLE_ALTERNATE_GEONAMEID,
                "name": SAMPLE_ALTERNATE_PRIMARY,
                "ascii_name": SAMPLE_ALTERNATE_PRIMARY,
                "country_code": SAMPLE_ALTERNATE_COUNTRY,
                "admin1_code": "09",
                "population": 194947,
                "name_type": "alternate",
            }
        ]

        state = {
            "libpostal_town": SAMPLE_ALTERNATE_INPUT,
            "postal_town_candidate": None,
            "postal_admin1_code": None,
            "postal_region": None,
            "country_code": SAMPLE_ALTERNATE_COUNTRY,
        }
        report("match input (alternate)", state)
        result = match(state)
        report("match output (alternate)", {
            "exact_match": result["exact_match"],
            "town_candidate": result["town_candidate"],
            "geonames_id": result["geonames_id"],
            "match_type": result["match_type"],
        })
        assert result["exact_match"] is True
        assert result["town_candidate"] == SAMPLE_ALTERNATE_INPUT  # input spelling preserved
        assert result["geonames_id"] == SAMPLE_ALTERNATE_GEONAMEID
        assert result["match_type"] == "alternate"
