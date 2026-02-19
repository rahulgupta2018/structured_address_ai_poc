"""Tests for geonames_exact service (Step 3) — disambiguation logic."""

from __future__ import annotations

from unittest.mock import patch

from services.geonames_exact import _disambiguate, match


# ── Fixtures ─────────────────────────────────────────────────────────────────

SPRINGFIELD_MO = {
    "geonameid": 4409896, "name": "Springfield", "ascii_name": "Springfield",
    "country_code": "US", "admin1_code": "MO", "population": 170188,
    "name_type": "primary",
}
SPRINGFIELD_MA = {
    "geonameid": 4951788, "name": "Springfield", "ascii_name": "Springfield",
    "country_code": "US", "admin1_code": "MA", "population": 154341,
    "name_type": "primary",
}
SPRINGFIELD_IL = {
    "geonameid": 4250542, "name": "Springfield", "ascii_name": "Springfield",
    "country_code": "US", "admin1_code": "IL", "population": 114394,
    "name_type": "primary",
}
SPRINGFIELD_OR = {
    "geonameid": 5754005, "name": "Springfield", "ascii_name": "Springfield",
    "country_code": "US", "admin1_code": "OR", "population": 60870,
    "name_type": "primary",
}

ALL_SPRINGFIELDS = [SPRINGFIELD_MO, SPRINGFIELD_MA, SPRINGFIELD_IL, SPRINGFIELD_OR]


# ── _disambiguate() unit tests ───────────────────────────────────────────────


class TestDisambiguate:
    """Test the core _disambiguate function."""

    def test_admin1_code_selects_correct_city(self):
        """With postal admin1=IL, should pick Springfield IL, not MO."""
        result = _disambiguate(ALL_SPRINGFIELDS, postal_admin1_code="IL", postal_region="Illinois")
        assert result["geonameid"] == SPRINGFIELD_IL["geonameid"]
        assert result["admin1_code"] == "IL"

    def test_admin1_code_ma(self):
        """With postal admin1=MA, should pick Springfield MA."""
        result = _disambiguate(ALL_SPRINGFIELDS, postal_admin1_code="MA", postal_region="Massachusetts")
        assert result["geonameid"] == SPRINGFIELD_MA["geonameid"]

    def test_no_postal_signal_falls_back_to_population(self):
        """Without postal signals, picks the largest city (MO)."""
        result = _disambiguate(ALL_SPRINGFIELDS, postal_admin1_code=None, postal_region=None)
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
        """Springfield + postal admin1=IL → picks IL, not MO."""
        mock_resolve_all.return_value = ALL_SPRINGFIELDS

        state = {
            "libpostal_town": "Springfield",
            "postal_town_candidate": None,
            "postal_admin1_code": "IL",
            "postal_region": "Illinois",
            "country_code": "US",
        }
        result = match(state)

        assert result["exact_match"] is True
        assert result["geonames_id"] == SPRINGFIELD_IL["geonameid"]
        assert result["town_candidate"] == "Springfield"
        assert result["match_type"] == "primary"

    @patch("services.geonames_exact.resolve_all_cities_by_name")
    def test_springfield_no_postal_signal_picks_largest(self, mock_resolve_all):
        """Springfield without postal signal → picks MO (largest)."""
        mock_resolve_all.return_value = ALL_SPRINGFIELDS

        state = {
            "libpostal_town": "Springfield",
            "postal_town_candidate": None,
            "postal_admin1_code": None,
            "postal_region": None,
            "country_code": "US",
        }
        result = match(state)

        assert result["exact_match"] is True
        assert result["geonames_id"] == SPRINGFIELD_MO["geonameid"]

    @patch("services.geonames_exact.resolve_all_cities_by_name")
    def test_unique_city_no_disambiguation_needed(self, mock_resolve_all):
        """Single match — no disambiguation logic triggered."""
        mock_resolve_all.return_value = [
            {
                "geonameid": 5391959,
                "name": "San Francisco",
                "ascii_name": "San Francisco",
                "country_code": "US",
                "admin1_code": "CA",
                "population": 864816,
                "name_type": "primary",
            }
        ]

        state = {
            "libpostal_town": "San Francisco",
            "postal_town_candidate": None,
            "postal_admin1_code": "CA",
            "postal_region": "California",
            "country_code": "US",
        }
        result = match(state)

        assert result["exact_match"] is True
        assert result["geonames_id"] == 5391959

    @patch("services.geonames_exact.resolve_all_cities_by_name", return_value=[])
    def test_no_match_returns_unmatched(self, mock_resolve_all):
        state = {
            "libpostal_town": "Xyzzyville",
            "postal_town_candidate": None,
            "postal_admin1_code": None,
            "postal_region": None,
            "country_code": "US",
        }
        result = match(state)

        assert result["exact_match"] is False
        assert result["geonames_id"] is None

    @patch("services.geonames_exact.resolve_all_cities_by_name")
    def test_alternate_name_preserves_input_spelling(self, mock_resolve_all):
        """Morbi (alternate name for Morvi) should preserve input spelling."""
        mock_resolve_all.return_value = [
            {
                "geonameid": 1262775,
                "name": "Morvi",
                "ascii_name": "Morvi",
                "country_code": "IN",
                "admin1_code": "09",
                "population": 194947,
                "name_type": "alternate",
            }
        ]

        state = {
            "libpostal_town": "Morbi",
            "postal_town_candidate": None,
            "postal_admin1_code": None,
            "postal_region": None,
            "country_code": "IN",
        }
        result = match(state)

        assert result["exact_match"] is True
        assert result["town_candidate"] == "Morbi"  # input spelling preserved
        assert result["geonames_id"] == 1262775
        assert result["match_type"] == "alternate"
