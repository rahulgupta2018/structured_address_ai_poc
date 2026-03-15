"""Tests for persistence service (Step 8) — final result assembly."""

from __future__ import annotations

from services.persistence import _compute_review_reason, persist

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
