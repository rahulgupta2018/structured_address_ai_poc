"""Tests for persistence service (Step 8) — final result assembly."""

from __future__ import annotations

from services.persistence import (
    _compute_review_reason,
    build_address_lines,
    country_code_to_name,
    persist,
)

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with your own data               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Validated address
SAMPLE_ADDRESS_1 = "123 Main St"
SAMPLE_COUNTRY_CODE = "US"
SAMPLE_TOWN = "Springfield"
SAMPLE_STREET = "Main St"
SAMPLE_BUILDING = "123"
SAMPLE_POSTAL_CODE = "62701"
SAMPLE_GEONAMES_ID = 4250542
SAMPLE_CONFIDENCE = 0.75

# Resolved address
SAMPLE_RESOLVED_TOWN = "London"
SAMPLE_RESOLVED_CONFIDENCE = 1.0

# Mismatch scenario
SAMPLE_MISMATCH_CONFIDENCE = 0.75
SAMPLE_MISMATCH_SUGGESTED_CC = "IT"


class TestPersist:
    def test_validated_status(self):
        state = {
            "status": "validated",
            "address_1": SAMPLE_ADDRESS_1,
            "address_2": None,
            "address_3": None,
            "country_code": SAMPLE_COUNTRY_CODE,
            "town_candidate": SAMPLE_TOWN,
            "libpostal_street": SAMPLE_STREET,
            "libpostal_building": SAMPLE_BUILDING,
            "libpostal_postal_code": SAMPLE_POSTAL_CODE,
            "confidence": SAMPLE_CONFIDENCE,
            "parser_source": "libpostal+llm",
            "exact_match": False,
            "scan_match": False,
            "geonames_id": SAMPLE_GEONAMES_ID,
            "warnings": [],
            "mismatch_detected": False,
            "suggested_country_code": None,
            "llm_calls": 1,
            "llm_prompt_tokens": 500,
            "llm_completion_tokens": 100,
        }
        report("persist input (validated)", {"status": state["status"], "town": state["town_candidate"], "confidence": state["confidence"]})
        result = persist(state)
        fr = result["final_result"]
        report("persist output (validated)", {
            "status": fr["status"],
            "town": fr["town"],
            "confidence_score": fr["confidence_score"],
            "geonames_id": fr["geonames_id"],
            "review_reason": fr["review_reason"],
        })
        assert fr["status"] == "validated"
        assert fr["town"] == SAMPLE_TOWN
        assert fr["country_code"] == SAMPLE_COUNTRY_CODE
        assert fr["confidence_score"] == SAMPLE_CONFIDENCE
        assert fr["geonames_id"] == SAMPLE_GEONAMES_ID
        assert fr["llm_calls"] == 1
        assert fr["review_reason"] is None
        # New regulatory fields
        assert fr["input_address_1"] == SAMPLE_ADDRESS_1
        assert fr["address_line_1"] == "123, Main St, 62701"
        assert fr["address_line_2"] == ""
        assert fr["country"] == "United States"

    def test_resolved_maps_to_validated(self):
        state = {
            "status": "resolved",
            "confidence": SAMPLE_RESOLVED_CONFIDENCE,
            "exact_match": True,
            "town_candidate": SAMPLE_RESOLVED_TOWN,
            "warnings": [],
        }
        result = persist(state)

        assert result["final_result"]["status"] == "validated"

    def test_needs_review_status(self):
        state = {
            "status": "needs_review",
            "raw_address": "Some address",
            "llm_result": {"town": "Unknown"},
            "confidence": 0.4,
            "warnings": ["geonames_no_match"],
        }
        report("persist input (needs_review)", {"status": state["status"], "confidence": state["confidence"]})
        result = persist(state)
        fr = result["final_result"]
        report("persist output (needs_review)", {
            "status": fr["status"],
            "review_reason": fr["review_reason"],
        })
        assert fr["status"] == "needs_review"
        assert fr["review_reason"] == "geonames_no_match"

    def test_rejected_status(self):
        state = {
            "status": "rejected",
            "confidence": 0.0,
            "warnings": [],
        }
        result = persist(state)

        assert result["final_result"]["status"] == "rejected"

    def test_unknown_status_maps_to_rejected(self):
        state = {"status": "something_else", "confidence": 0.0, "warnings": []}
        result = persist(state)
        assert result["final_result"]["status"] == "rejected"

    def test_no_llm_usage_zeros(self):
        state = {
            "status": "resolved",
            "confidence": 1.0,
            "exact_match": True,
            "warnings": [],
        }
        result = persist(state)
        fr = result["final_result"]

        assert fr["llm_calls"] == 0
        assert fr["llm_prompt_tokens"] == 0
        assert fr["llm_completion_tokens"] == 0

    def test_warnings_joined(self):
        state = {
            "status": "needs_review",
            "warnings": ["warn_a", "warn_b", "warn_c"],
            "confidence": 0.0,
        }
        result = persist(state)

        assert result["final_result"]["warnings"] == "warn_a; warn_b; warn_c"

    def test_geonames_match_flag(self):
        state = {
            "status": "resolved",
            "exact_match": True,
            "scan_match": False,
            "confidence": 1.0,
            "warnings": [],
        }
        result = persist(state)
        assert result["final_result"]["geonames_match"] is True

    def test_geonames_match_via_scan(self):
        state = {
            "status": "resolved",
            "exact_match": False,
            "scan_match": True,
            "confidence": 0.8,
            "warnings": [],
        }
        result = persist(state)
        assert result["final_result"]["geonames_match"] is True

    def test_mismatch_info_included(self):
        state = {
            "status": "validated",
            "confidence": SAMPLE_MISMATCH_CONFIDENCE,
            "mismatch_detected": True,
            "suggested_country_code": SAMPLE_MISMATCH_SUGGESTED_CC,
            "warnings": [],
        }
        report("persist input (mismatch)", {"mismatch_detected": True, "suggested_cc": SAMPLE_MISMATCH_SUGGESTED_CC})
        result = persist(state)
        fr = result["final_result"]
        report("persist output (mismatch)", {
            "mismatch_detected": fr["mismatch_detected"],
            "suggested_country_code": fr["suggested_country_code"],
        })
        assert fr["mismatch_detected"] is True
        assert fr["suggested_country_code"] == SAMPLE_MISMATCH_SUGGESTED_CC

    def test_confidence_rounded(self):
        state = {
            "status": "validated",
            "confidence": 0.123456789,
            "warnings": [],
        }
        result = persist(state)
        assert result["final_result"]["confidence_score"] == 0.1235


class TestComputeReviewReason:
    def test_validated_returns_none(self):
        assert _compute_review_reason({}, "validated") is None

    def test_no_address_data(self):
        state = {"raw_address": ""}
        assert _compute_review_reason(state, "needs_review") == "no_address_data"

    def test_no_raw_address_key(self):
        state = {}
        assert _compute_review_reason(state, "needs_review") == "no_address_data"

    def test_llm_result_present(self):
        state = {
            "raw_address": "some text",
            "status": "needs_review",
            "llm_result": {"town": "X"},
        }
        assert _compute_review_reason(state, "needs_review") == "geonames_no_match"

    def test_no_llm_result(self):
        state = {
            "raw_address": "some text",
            "status": "needs_review",
        }
        assert _compute_review_reason(state, "needs_review") == "no_town_candidate"

    def test_other_status(self):
        state = {"raw_address": "some text"}
        assert _compute_review_reason(state, "rejected") == "no_town_candidate"


class TestBuildAddressLines:
    """Regulatory address-line builder: 2 lines × 70 chars, no town/country."""

    def test_short_address_fits_line1(self):
        line1, line2 = build_address_lines("123", "Main St", "62701")
        assert line1 == "123, Main St, 62701"
        assert line2 == ""

    def test_empty_parts(self):
        line1, line2 = build_address_lines(None, None, None)
        assert line1 == ""
        assert line2 == ""

    def test_only_street(self):
        line1, line2 = build_address_lines(None, "Baker Street", None)
        assert line1 == "Baker Street"
        assert line2 == ""

    def test_only_building(self):
        line1, line2 = build_address_lines("42A", None, None)
        assert line1 == "42A"
        assert line2 == ""

    def test_only_postal(self):
        line1, line2 = build_address_lines(None, None, "SW1A 1AA")
        assert line1 == "SW1A 1AA"
        assert line2 == ""

    def test_exactly_70_chars(self):
        street = "A" * 66  # "building, " is skipped; "A"*66 + ", " + "12" = 70
        line1, line2 = build_address_lines(None, street, "12")
        combined = f"{street}, 12"
        assert len(combined) == 70
        assert line1 == combined
        assert line2 == ""

    def test_overflow_splits_at_word_boundary(self):
        # Build a street that, with building + postal, exceeds 70 characters
        building = "Building 42"
        street = "Avenida de la Constitucion de los Trabajadores Autonomos"
        postal = "28001"
        line1, line2 = build_address_lines(building, street, postal)
        assert len(line1) <= 70
        assert len(line2) <= 70
        # Line1 should not cut a word
        assert not line1.endswith(" ")
        # All parts should appear across line1 + line2
        combined = line1 + " " + line2
        assert "Building 42" in combined
        assert "28001" in combined

    def test_line1_max_70_chars(self):
        long_street = "Via Alessandro Manzoni di Santa Maria della Misericordia Numero Civico"
        line1, line2 = build_address_lines("100", long_street, "20121")
        assert len(line1) <= 70
        assert len(line2) <= 70

    def test_long_single_word_does_not_crash(self):
        monster = "A" * 80  # single token > 70 chars; no word boundary
        line1, line2 = build_address_lines(None, monster, None)
        assert len(line1) <= 80  # falls back to full limit cut
        assert line2 == "" or len(line2) <= 70

    def test_blank_strings_treated_as_empty(self):
        line1, line2 = build_address_lines("  ", "  ", "  ")
        assert line1 == ""
        assert line2 == ""

    def test_strips_whitespace(self):
        line1, line2 = build_address_lines(" 42B ", " High Street ", " E1 6AN ")
        assert line1 == "42B, High Street, E1 6AN"
        assert line2 == ""


class TestCountryCodeToName:
    """country_code_to_name() utility — ISO alpha-2 → common English name."""

    def test_known_code(self):
        assert country_code_to_name("US") == "United States"

    def test_lowercase_code(self):
        assert country_code_to_name("gb") == "United Kingdom"

    def test_unknown_code(self):
        assert country_code_to_name("ZZ") is None

    def test_none_code(self):
        assert country_code_to_name(None) is None

    def test_empty_code(self):
        assert country_code_to_name("") is None

    def test_de(self):
        assert country_code_to_name("DE") == "Germany"

    def test_it(self):
        assert country_code_to_name("IT") == "Italy"

    def test_th(self):
        assert country_code_to_name("TH") == "Thailand"


class TestPersistNewFields:
    """Verify the new regulatory fields in final_result."""

    def test_country_field_populated(self):
        state = {
            "status": "resolved",
            "confidence": 1.0,
            "country_code": "FR",
            "exact_match": True,
            "town_candidate": "Paris",
            "warnings": [],
        }
        fr = persist(state)["final_result"]
        assert fr["country"] == "France"

    def test_country_field_none_when_no_code(self):
        state = {
            "status": "rejected",
            "confidence": 0.0,
            "warnings": [],
        }
        fr = persist(state)["final_result"]
        assert fr["country"] is None

    def test_address_lines_from_extracted_parts(self):
        state = {
            "status": "resolved",
            "confidence": 1.0,
            "country_code": "DE",
            "exact_match": True,
            "town_candidate": "Berlin",
            "libpostal_building": "14",
            "libpostal_street": "Friedrichstrasse",
            "libpostal_postal_code": "10117",
            "warnings": [],
        }
        fr = persist(state)["final_result"]
        assert fr["address_line_1"] == "14, Friedrichstrasse, 10117"
        assert fr["address_line_2"] == ""
        assert len(fr["address_line_1"]) <= 70

    def test_address_lines_empty_when_no_parts(self):
        state = {
            "status": "needs_review",
            "confidence": 0.0,
            "warnings": [],
        }
        fr = persist(state)["final_result"]
        assert fr["address_line_1"] == ""
        assert fr["address_line_2"] == ""

    def test_original_input_preserved_as_input_address(self):
        state = {
            "status": "resolved",
            "confidence": 1.0,
            "address_1": "123 High St",
            "address_2": "Flat 4",
            "address_3": "London",
            "country_code": "GB",
            "exact_match": True,
            "town_candidate": "London",
            "warnings": [],
        }
        fr = persist(state)["final_result"]
        assert fr["input_address_1"] == "123 High St"
        assert fr["input_address_2"] == "Flat 4"
        assert fr["input_address_3"] == "London"

    def test_long_address_splits_across_two_lines(self):
        state = {
            "status": "resolved",
            "confidence": 1.0,
            "country_code": "ES",
            "exact_match": True,
            "town_candidate": "Madrid",
            "libpostal_building": "Edificio Comercial Torres de Colon Planta 12 Oficina 3B",
            "libpostal_street": "Avenida de la Constitucion",
            "libpostal_postal_code": "28001",
            "warnings": [],
        }
        fr = persist(state)["final_result"]
        assert len(fr["address_line_1"]) <= 70
        assert len(fr["address_line_2"]) <= 70
        # Both together should contain all parts
        joined = fr["address_line_1"] + " " + fr["address_line_2"]
        assert "28001" in joined
