"""Tests for libpostal_parser service (Step 1) — address parsing."""

from __future__ import annotations

from unittest.mock import patch

import services.libpostal_parser as lp_module
from services.libpostal_parser import parse

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with sample data                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Full address for unavailability / basic tests
SAMPLE_RAW_ADDRESS = "123 Main St, Springfield, IL 62701"

# Simulated libpostal parse output for city extraction test
# Change these to match your address — values are (token, label) pairs
SAMPLE_PARSE_CITY = [("springfield", "city"), ("123 main st", "road"), ("62701", "postcode")]
EXPECTED_TOWN = "springfield"
EXPECTED_STREET = "123 main st"
EXPECTED_POSTAL_CODE = "62701"

# Simulated parse output for building extraction test
SAMPLE_PARSE_BUILDING = [("10", "house_number"), ("high street", "road"), ("london", "city")]
EXPECTED_BUILDING = "10"
EXPECTED_BUILDING_TOWN = "london"
EXPECTED_BUILDING_STREET = "high street"

# Simulated parse output for multiple city candidates
SAMPLE_PARSE_MULTI_CITY = [("downtown", "suburb"), ("springfield", "city")]
EXPECTED_MULTI_CITY_WINNER = "springfield"


class TestParseWithoutLibpostal:
    """Tests that run when libpostal is NOT available."""

    @patch.object(lp_module, "LIBPOSTAL_AVAILABLE", False)
    def test_returns_warning_when_unavailable(self):
        state = {"raw_address": SAMPLE_RAW_ADDRESS}
        report("parse input", state)
        result = parse(state)
        report("parse output (libpostal unavailable)", {
            "libpostal_town": result["libpostal_town"],
            "warnings": result.get("warnings", []),
        })
        assert result["libpostal_town"] is None
        assert result["libpostal_postal_code"] is None
        assert result["libpostal_street"] is None
        assert result["libpostal_building"] is None
        assert "libpostal_not_installed" in result.get("warnings", [])

    @patch.object(lp_module, "LIBPOSTAL_AVAILABLE", True)
    def test_empty_address_warns(self):
        state = {"raw_address": ""}
        result = parse(state)

        assert result["libpostal_town"] is None
        assert "empty_address" in result.get("warnings", [])

    @patch.object(lp_module, "LIBPOSTAL_AVAILABLE", True)
    def test_whitespace_only_address_warns(self):
        state = {"raw_address": "   "}
        result = parse(state)

        assert result["libpostal_town"] is None
        assert "empty_address" in result.get("warnings", [])


class TestParseWithMockedLibpostal:
    """Tests with mocked libpostal parse results."""

    @patch.object(lp_module, "LIBPOSTAL_AVAILABLE", True)
    @patch.object(lp_module, "_postal_parse")
    def test_extracts_city(self, mock_parse):
        mock_parse.return_value = SAMPLE_PARSE_CITY
        state = {"raw_address": SAMPLE_RAW_ADDRESS}
        report("parse input", {"raw_address": SAMPLE_RAW_ADDRESS, "mock_tokens": SAMPLE_PARSE_CITY})
        result = parse(state)
        report("parse output", {
            "libpostal_town": result["libpostal_town"],
            "libpostal_street": result["libpostal_street"],
            "libpostal_postal_code": result["libpostal_postal_code"],
            "libpostal_building": result.get("libpostal_building"),
        })
        assert result["libpostal_town"] == EXPECTED_TOWN
        assert result["libpostal_street"] == EXPECTED_STREET
        assert result["libpostal_postal_code"] == EXPECTED_POSTAL_CODE

    @patch.object(lp_module, "LIBPOSTAL_AVAILABLE", True)
    @patch.object(lp_module, "_postal_parse")
    def test_extracts_building(self, mock_parse):
        mock_parse.return_value = SAMPLE_PARSE_BUILDING
        state = {"raw_address": SAMPLE_RAW_ADDRESS}
        report("parse input", {"raw_address": SAMPLE_RAW_ADDRESS, "mock_tokens": SAMPLE_PARSE_BUILDING})
        result = parse(state)
        report("parse output", {
            "libpostal_building": result["libpostal_building"],
            "libpostal_town": result["libpostal_town"],
            "libpostal_street": result["libpostal_street"],
        })
        assert result["libpostal_building"] == EXPECTED_BUILDING
        assert result["libpostal_town"] == EXPECTED_BUILDING_TOWN
        assert result["libpostal_street"] == EXPECTED_BUILDING_STREET

    @patch.object(lp_module, "LIBPOSTAL_AVAILABLE", True)
    @patch.object(lp_module, "_postal_parse")
    def test_no_city_label_warns(self, mock_parse):
        mock_parse.return_value = [
            ("123 main st", "road"),
            ("62701", "postcode"),
        ]
        state = {"raw_address": "123 Main St 62701"}
        result = parse(state)

        assert result["libpostal_town"] is None
        assert "libpostal_no_city_label" in result.get("warnings", [])

    @patch.object(lp_module, "LIBPOSTAL_AVAILABLE", True)
    @patch.object(lp_module, "_postal_parse")
    def test_multiple_city_candidates_prefers_city_label(self, mock_parse):
        mock_parse.return_value = SAMPLE_PARSE_MULTI_CITY
        state = {"raw_address": SAMPLE_RAW_ADDRESS}
        report("parse input", {"raw_address": SAMPLE_RAW_ADDRESS, "mock_tokens": SAMPLE_PARSE_MULTI_CITY})
        result = parse(state)
        report("parse output", {
            "libpostal_town": result["libpostal_town"],
            "warnings": result.get("warnings", []),
        })
        assert result["libpostal_town"] == EXPECTED_MULTI_CITY_WINNER
        assert "multiple_town_candidates" in result.get("warnings", [])

    @patch.object(lp_module, "LIBPOSTAL_AVAILABLE", True)
    @patch.object(lp_module, "_postal_parse")
    def test_multiple_city_candidates_fallback_to_first(self, mock_parse):
        mock_parse.return_value = [
            ("district a", "city_district"),
            ("suburb b", "suburb"),
        ]
        state = {"raw_address": "District A Suburb B"}
        result = parse(state)

        # No explicit "city" label → falls back to first candidate
        assert result["libpostal_town"] == "district a"
        assert "multiple_town_candidates" in result.get("warnings", [])

    @patch.object(lp_module, "LIBPOSTAL_AVAILABLE", True)
    @patch.object(lp_module, "_postal_parse")
    def test_parse_error_warns(self, mock_parse):
        mock_parse.side_effect = RuntimeError("libpostal crash")
        state = {"raw_address": "Some address"}
        result = parse(state)

        assert result["libpostal_town"] is None
        assert "libpostal_parse_error" in result.get("warnings", [])

    @patch.object(lp_module, "LIBPOSTAL_AVAILABLE", True)
    @patch.object(lp_module, "_postal_parse")
    def test_empty_values_skipped(self, mock_parse):
        mock_parse.return_value = [
            ("", "city"),
            ("main st", "road"),
        ]
        state = {"raw_address": "Main St"}
        result = parse(state)

        assert result["libpostal_town"] is None
        assert "libpostal_no_city_label" in result.get("warnings", [])

    @patch.object(lp_module, "LIBPOSTAL_AVAILABLE", True)
    @patch.object(lp_module, "_postal_parse")
    def test_defaults_set(self, mock_parse):
        mock_parse.return_value = []
        state = {"raw_address": "test"}
        result = parse(state)

        assert "libpostal_town" in result
        assert "libpostal_postal_code" in result
        assert "libpostal_street" in result
        assert "libpostal_building" in result
