"""Tests for mismatch_detector service (Step 4) — country-code mismatch detection."""

from __future__ import annotations

from unittest.mock import patch

from services.mismatch_detector import detect

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with sample data                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Mismatch test: town exists in CORRECT country but NOT in WRONG country
SAMPLE_MISMATCH_TOWN = "Barisardo"
SAMPLE_WRONG_COUNTRY = "IE"       # stated country (wrong)
SAMPLE_CORRECT_COUNTRY = "IT"     # actual country (correct)
SAMPLE_CORRECT_GEONAMEID = 3182568
SAMPLE_CORRECT_POPULATION = 3800

# No-mismatch test: town that exists in its stated country
SAMPLE_VALID_TOWN = "London"
SAMPLE_VALID_COUNTRY = "GB"


# ── Fixtures ───────────────────────────────────────────────────────────────

BARISARDO_IT = {
    "country_code": SAMPLE_CORRECT_COUNTRY,
    "geonameid": SAMPLE_CORRECT_GEONAMEID,
    "name": SAMPLE_MISMATCH_TOWN,
    "population": SAMPLE_CORRECT_POPULATION,
    "name_type": "primary",
}


class TestMismatchDetector:
    def test_exact_match_skips_detection(self):
        """If exact_match is True, mismatch detection is skipped."""
        state = {
            "exact_match": True,
            "libpostal_town": SAMPLE_VALID_TOWN,
            "country_code": SAMPLE_VALID_COUNTRY,
        }
        report("detect input (exact match)", state)
        result = detect(state)
        report("detect output (skipped)", {"mismatch_detected": result["mismatch_detected"]})
        assert result["mismatch_detected"] is False
        assert result["suggested_country_code"] is None

    def test_no_candidate_skips(self):
        """No town candidate — nothing to check."""
        state = {
            "exact_match": False,
            "libpostal_town": None,
            "town_candidate": None,
            "country_code": "US",
        }
        result = detect(state)
        assert result["mismatch_detected"] is False

    @patch("services.mismatch_detector.list_countries_for_city", return_value=[])
    def test_town_not_in_any_country(self, mock_list):
        """Town doesn't exist anywhere → no mismatch."""
        state = {
            "exact_match": False,
            "libpostal_town": "Xyzzyville",
            "country_code": "US",
        }
        result = detect(state)
        assert result["mismatch_detected"] is False

    @patch("services.mismatch_detector.list_countries_for_city")
    def test_mismatch_detected(self, mock_list):
        """Town exists in CORRECT country but stated country is WRONG → mismatch."""
        mock_list.return_value = [BARISARDO_IT]

        state = {
            "exact_match": False,
            "libpostal_town": SAMPLE_MISMATCH_TOWN,
            "country_code": SAMPLE_WRONG_COUNTRY,
        }
        report("detect input (mismatch)", state)
        result = detect(state)
        report("detect output (mismatch)", {
            "mismatch_detected": result["mismatch_detected"],
            "suggested_country_code": result["suggested_country_code"],
        })
        assert result["mismatch_detected"] is True
        assert result["suggested_country_code"] == SAMPLE_CORRECT_COUNTRY

    @patch("services.mismatch_detector.list_countries_for_city")
    def test_no_mismatch_when_town_in_stated_country(self, mock_list):
        """Town exists in stated country → no mismatch."""
        london_gb = {
            "country_code": SAMPLE_VALID_COUNTRY,
            "geonameid": 2643743,
            "name": SAMPLE_VALID_TOWN,
            "population": 7556900,
            "name_type": "primary",
        }
        london_ca = {
            "country_code": "CA",
            "geonameid": 6058560,
            "name": SAMPLE_VALID_TOWN,
            "population": 346765,
            "name_type": "primary",
        }
        mock_list.return_value = [london_gb, london_ca]

        state = {
            "exact_match": False,
            "libpostal_town": SAMPLE_VALID_TOWN,
            "country_code": SAMPLE_VALID_COUNTRY,
        }
        report("detect input (no mismatch)", state)
        result = detect(state)
        report("detect output (no mismatch)", {"mismatch_detected": result["mismatch_detected"]})
        assert result["mismatch_detected"] is False

    @patch("services.mismatch_detector.list_countries_for_city")
    def test_picks_highest_population(self, mock_list):
        """When multiple other countries match, pick the one with the highest population."""
        city_us = {
            "country_code": "US",
            "geonameid": 1,
            "name": "TestCity",
            "population": 100000,
            "name_type": "primary",
        }
        city_mx = {
            "country_code": "MX",
            "geonameid": 2,
            "name": "TestCity",
            "population": 50000,
            "name_type": "primary",
        }
        mock_list.return_value = [city_us, city_mx]

        state = {
            "exact_match": False,
            "libpostal_town": "TestCity",
            "country_code": "CA",
        }
        report("detect input (highest pop)", state)
        result = detect(state)
        report("detect output (highest pop)", {
            "mismatch_detected": result["mismatch_detected"],
            "suggested_country_code": result["suggested_country_code"],
        })
        assert result["mismatch_detected"] is True
        assert result["suggested_country_code"] == "US"

    @patch("services.mismatch_detector.list_countries_for_city")
    def test_falls_back_to_town_candidate(self, mock_list):
        """Uses town_candidate if libpostal_town is None."""
        mock_list.return_value = [BARISARDO_IT]

        state = {
            "exact_match": False,
            "libpostal_town": None,
            "town_candidate": SAMPLE_MISMATCH_TOWN,
            "country_code": SAMPLE_WRONG_COUNTRY,
        }
        result = detect(state)

        assert result["mismatch_detected"] is True
        assert result["suggested_country_code"] == SAMPLE_CORRECT_COUNTRY

    @patch("services.mismatch_detector.list_countries_for_city", return_value=[])
    def test_empty_country_code(self, mock_list):
        state = {
            "exact_match": False,
            "libpostal_town": "London",
            "country_code": "",
        }
        result = detect(state)
        assert result["mismatch_detected"] is False
