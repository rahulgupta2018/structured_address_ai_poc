"""Tests for geonames_revalidation service (Step 7) — re-validation of resolved towns."""

from __future__ import annotations

from unittest.mock import patch

from services.geonames_revalidation import _prefer_address_spelling, revalidate
from utils.config import (
    CONFIDENCE_LLM_CONFIRMED,
    CONFIDENCE_LLM_FUZZY_CONFIRMED,
    CONFIDENCE_LLM_UNVERIFIED,
)

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with your own data               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# City with alternate name (LLM returns primary, address has alternate)
SAMPLE_LLM_TOWN = "Morvi"                 # what the LLM returns (GeoNames primary)
SAMPLE_LLM_COUNTRY = "IN"
SAMPLE_LLM_GEONAMEID = 1262775
SAMPLE_LLM_RAW_ADDRESS = "MORBI GUJR"     # raw address (contains alternate spelling)
SAMPLE_LLM_ADDRESS_SPELLING = "Morbi"     # address-text spelling to prefer

# Mismatch scenario: town in wrong country
SAMPLE_MISMATCH_TOWN = "Barisardo"
SAMPLE_MISMATCH_WRONG_CC = "IE"           # stated country (wrong)
SAMPLE_MISMATCH_CORRECT_CC = "IT"         # actual country
SAMPLE_MISMATCH_GEONAMEID = 3182568
SAMPLE_MISMATCH_RAW_ADDRESS = "VIA ROMA 1, BARISARDO"

# Postal fallback scenario
SAMPLE_POSTAL_FALLBACK_TOWN = "Barisardo"
SAMPLE_POSTAL_FALLBACK_PLACE = "Bari Sardo"  # place_name in postal DB
SAMPLE_POSTAL_FALLBACK_CC = "IT"


# ── Fixtures ─────────────────────────────────────────────────────────────────

MORVI_CITY = {
    "geonameid": SAMPLE_LLM_GEONAMEID,
    "name": SAMPLE_LLM_TOWN,
    "ascii_name": SAMPLE_LLM_TOWN,
    "country_code": SAMPLE_LLM_COUNTRY,
    "admin1_code": "09",
    "population": 194947,
    "name_type": "primary",
}

BARISARDO_CITY = {
    "geonameid": SAMPLE_MISMATCH_GEONAMEID,
    "name": SAMPLE_MISMATCH_TOWN,
    "ascii_name": SAMPLE_MISMATCH_TOWN,
    "country_code": SAMPLE_MISMATCH_CORRECT_CC,
    "admin1_code": "88",
    "population": 3800,
    "name_type": "primary",
}


class TestRevalidateDeterministic:
    """Deterministic-path rows should pass through."""

    def test_resolved_status_passthrough(self):
        state = {
            "status": "resolved",
            "match_confidence": 1.0,
        }
        report("revalidate input (resolved)", state)
        result = revalidate(state)
        report("revalidate output (resolved)", {"confidence": result["confidence"]})
        assert result["confidence"] == 1.0

    def test_resolved_with_zero_confidence(self):
        state = {
            "status": "resolved",
            "match_confidence": 0.0,
        }
        result = revalidate(state)
        assert result["confidence"] == 0.0


class TestRevalidateLLM:
    """LLM-path rows — validate the LLM result against GeoNames."""

    def test_no_llm_result_needs_review(self):
        state = {"status": "pending", "llm_result": None}
        result = revalidate(state)
        assert result["confidence"] == 0.0
        assert result["status"] == "needs_review"

    def test_llm_result_not_dict_needs_review(self):
        state = {"status": "pending", "llm_result": "some string"}
        result = revalidate(state)
        assert result["confidence"] == 0.0
        assert result["status"] == "needs_review"

    def test_empty_town_needs_review(self):
        state = {
            "status": "pending",
            "llm_result": {"town": ""},
            "country_code": "US",
        }
        result = revalidate(state)
        assert result["confidence"] == CONFIDENCE_LLM_UNVERIFIED
        assert result["status"] == "needs_review"

    @patch("services.geonames_revalidation.resolve_city_by_name", return_value=MORVI_CITY)
    @patch("services.geonames_revalidation._prefer_address_spelling", return_value=SAMPLE_LLM_TOWN)
    def test_exact_match_in_country(self, mock_prefer, mock_resolve):
        state = {
            "status": "pending",
            "llm_result": {"town": SAMPLE_LLM_TOWN},
            "country_code": SAMPLE_LLM_COUNTRY,
            "raw_address": SAMPLE_LLM_RAW_ADDRESS,
        }
        report("revalidate input (exact match)", {"llm_town": SAMPLE_LLM_TOWN, "country": SAMPLE_LLM_COUNTRY})
        result = revalidate(state)
        report("revalidate output (exact match)", {
            "confidence": result["confidence"],
            "status": result["status"],
            "geonames_id": result["geonames_id"],
        })
        assert result["confidence"] == CONFIDENCE_LLM_CONFIRMED
        assert result["status"] == "validated"
        assert result["geonames_id"] == SAMPLE_LLM_GEONAMEID

    @patch("services.geonames_revalidation.resolve_city_by_name")
    def test_match_in_suggested_country(self, mock_resolve):
        """Town not in stated country, but found in suggested country."""
        # First call (stated country) returns None, second (suggested) returns city
        mock_resolve.side_effect = [None, BARISARDO_CITY]

        state = {
            "status": "pending",
            "llm_result": {"town": SAMPLE_MISMATCH_TOWN},
            "country_code": SAMPLE_MISMATCH_WRONG_CC,
            "suggested_country_code": SAMPLE_MISMATCH_CORRECT_CC,
            "raw_address": SAMPLE_MISMATCH_RAW_ADDRESS,
        }
        report("revalidate input (suggested country)", {"llm_town": SAMPLE_MISMATCH_TOWN, "stated_cc": SAMPLE_MISMATCH_WRONG_CC, "suggested_cc": SAMPLE_MISMATCH_CORRECT_CC})
        result = revalidate(state)
        report("revalidate output (suggested country)", {
            "confidence": result["confidence"],
            "status": result["status"],
            "mismatch_detected": result["mismatch_detected"],
            "suggested_country_code": result["suggested_country_code"],
        })
        assert result["confidence"] == CONFIDENCE_LLM_CONFIRMED
        assert result["status"] == "validated"
        assert result["mismatch_detected"] is True
        assert result["suggested_country_code"] == SAMPLE_MISMATCH_CORRECT_CC

    @patch("services.geonames_revalidation.search_postal_by_place_name", return_value=[])
    @patch("services.geonames_revalidation.list_countries_for_city", return_value=[])
    @patch("services.geonames_revalidation._fuzzy_revalidate", return_value=None)
    @patch("services.geonames_revalidation.resolve_city_by_name", return_value=None)
    def test_no_match_anywhere_needs_review(
        self, mock_resolve, mock_fuzzy, mock_list, mock_postal
    ):
        state = {
            "status": "pending",
            "llm_result": {"town": "UnknownTown"},
            "country_code": "US",
            "raw_address": "123 MAIN ST UNKNOWNTOWN",
        }
        report("revalidate input (no match)", {"llm_town": "UnknownTown", "country": "US"})
        result = revalidate(state)
        report("revalidate output (no match)", {
            "confidence": result["confidence"],
            "status": result["status"],
            "warnings": result.get("warnings", []),
            "town_candidate": result["town_candidate"],
        })
        assert result["confidence"] == CONFIDENCE_LLM_UNVERIFIED
        assert result["status"] == "needs_review"
        assert "geonames_no_match" in result.get("warnings", [])
        assert result["town_candidate"] == "UnknownTown"

    @patch("services.geonames_revalidation.search_postal_by_place_name", return_value=[])
    @patch("services.geonames_revalidation.list_countries_for_city", return_value=[])
    @patch("services.geonames_revalidation._fuzzy_revalidate")
    @patch("services.geonames_revalidation.resolve_city_by_name", return_value=None)
    def test_fuzzy_match_validates(self, mock_resolve, mock_fuzzy, mock_list, mock_postal):
        mock_fuzzy.return_value = {
            "geonameid": 9999,
            "name": "FuzzyCity",
        }
        state = {
            "status": "pending",
            "llm_result": {"town": "FuzzyCiti"},
            "country_code": "US",
            "raw_address": "123 FUZZY ST",
        }
        report("revalidate input (fuzzy)", {"llm_town": "FuzzyCiti", "country": "US"})
        result = revalidate(state)
        report("revalidate output (fuzzy)", {
            "confidence": result["confidence"],
            "status": result["status"],
            "town_candidate": result["town_candidate"],
        })
        assert result["confidence"] == CONFIDENCE_LLM_FUZZY_CONFIRMED
        assert result["status"] == "validated"
        assert result["town_candidate"] == "FuzzyCity"

    @patch("services.geonames_revalidation.search_postal_by_place_name", return_value=[])
    @patch("services.geonames_revalidation.list_countries_for_city")
    @patch("services.geonames_revalidation._fuzzy_revalidate", return_value=None)
    @patch("services.geonames_revalidation.resolve_city_by_name")
    def test_cross_country_fallback(self, mock_resolve, mock_fuzzy, mock_list, mock_postal):
        """cross-country check: town not in stated country but found in another."""
        # resolve_city_by_name: first call (IE/stated) → None, second (IT/cross-country) → city
        mock_resolve.side_effect = [None, BARISARDO_CITY]
        mock_list.return_value = [
            {"country_code": SAMPLE_MISMATCH_CORRECT_CC, "geonameid": SAMPLE_MISMATCH_GEONAMEID,
             "name": SAMPLE_MISMATCH_TOWN, "population": 3800, "name_type": "primary"}
        ]

        state = {
            "status": "pending",
            "llm_result": {"town": SAMPLE_MISMATCH_TOWN},
            "country_code": SAMPLE_MISMATCH_WRONG_CC,
            "suggested_country_code": None,
            "raw_address": SAMPLE_MISMATCH_TOWN.upper(),
        }
        report("revalidate input (cross-country)", {"llm_town": SAMPLE_MISMATCH_TOWN, "stated_cc": SAMPLE_MISMATCH_WRONG_CC})
        result = revalidate(state)
        report("revalidate output (cross-country)", {
            "status": result["status"],
            "mismatch_detected": result["mismatch_detected"],
            "suggested_country_code": result["suggested_country_code"],
        })
        assert result["status"] == "validated"
        assert result["mismatch_detected"] is True
        assert result["suggested_country_code"] == SAMPLE_MISMATCH_CORRECT_CC

    @patch("services.geonames_revalidation.list_countries_for_city", return_value=[])
    @patch("services.geonames_revalidation._fuzzy_revalidate", return_value=None)
    @patch("services.geonames_revalidation.resolve_city_by_name", return_value=None)
    def test_postal_fallback(self, mock_resolve, mock_fuzzy, mock_list):
        """Town found in postal-codes table as fallback."""
        with patch("services.geonames_revalidation.search_postal_by_place_name") as mock_postal:
            mock_postal.return_value = [
                {
                    "postal_code": "08042",
                    "place_name": SAMPLE_POSTAL_FALLBACK_PLACE,
                    "admin_name1": "Sardinia",
                    "admin_code1": "88",
                    "country_code": SAMPLE_POSTAL_FALLBACK_CC,
                    "latitude": 39.84,
                    "longitude": 9.65,
                }
            ]
            state = {
                "status": "pending",
                "llm_result": {"town": SAMPLE_POSTAL_FALLBACK_TOWN},
                "country_code": SAMPLE_MISMATCH_WRONG_CC,
                "raw_address": SAMPLE_POSTAL_FALLBACK_TOWN.upper(),
            }
            report("revalidate input (postal fallback)", {"llm_town": SAMPLE_POSTAL_FALLBACK_TOWN, "stated_cc": SAMPLE_MISMATCH_WRONG_CC})
            result = revalidate(state)
            report("revalidate output (postal fallback)", {
                "status": result["status"],
                "town_candidate": result["town_candidate"],
                "mismatch_detected": result["mismatch_detected"],
                "suggested_country_code": result["suggested_country_code"],
            })
            assert result["status"] == "validated"
            assert result["town_candidate"] == SAMPLE_POSTAL_FALLBACK_PLACE
            assert result["mismatch_detected"] is True
            assert result["suggested_country_code"] == SAMPLE_POSTAL_FALLBACK_CC


class TestPreferAddressSpelling:
    @patch("services.geonames_revalidation.resolve_city_by_name")
    def test_prefers_address_token(self, mock_resolve):
        """Address spelling differs from LLM → prefer address spelling."""
        morbi_alt = dict(MORVI_CITY, name_type="alternate")
        # Only respond to the target token; return None for other tokens
        def _side_effect(_cc, token):
            return morbi_alt if token == SAMPLE_LLM_ADDRESS_SPELLING.lower() else None
        mock_resolve.side_effect = _side_effect

        result = _prefer_address_spelling(
            SAMPLE_LLM_TOWN, MORVI_CITY,
            f"SHOP 10, {SAMPLE_LLM_ADDRESS_SPELLING.upper()}, GUJR",
            SAMPLE_LLM_COUNTRY,
        )
        report("_prefer_address_spelling", {
            "llm_town": SAMPLE_LLM_TOWN,
            "raw_address": f"SHOP 10, {SAMPLE_LLM_ADDRESS_SPELLING.upper()}, GUJR",
            "preferred": result,
        })
        assert result == SAMPLE_LLM_ADDRESS_SPELLING

    def test_no_raw_address(self):
        result = _prefer_address_spelling(SAMPLE_LLM_TOWN, MORVI_CITY, "", SAMPLE_LLM_COUNTRY)
        assert result == SAMPLE_LLM_TOWN

    def test_no_geonameid(self):
        city = {"name": "Test"}
        result = _prefer_address_spelling("Test", city, "TEST ADDR", "US")
        assert result == "Test"
