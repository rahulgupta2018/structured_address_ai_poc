"""End-to-end tests for Flow 1: Steps 0→1→2→3→8.

Flow 1 is the fastest deterministic path: preprocess → parse →
postal lookup → exact match → persist. Addresses resolve at Step 3
with an exact GeoNames match (confidence ≥ 0.95).

All tests call real service functions against the real GeoNames SQLite
database — zero mocks. Sample data and expected results are defined as
top-level constants. To test with different addresses, edit the constants
only — no changes inside test classes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.normalizer import preprocess
from services.libpostal_parser import parse
from services.postal_lookup import lookup
from services.geonames_exact import match
from services.persistence import persist
from utils.config import CONFIDENCE_EXACT_ALTERNATE, CONFIDENCE_EXACT_PRIMARY

from tests.test_services.report import report


# ======================================================================
# SAMPLE DATA & EXPECTED RESULTS — edit here, not inside tests
# ======================================================================

# -- Flow 1 Happy Path: addresses that resolve at Step 3 --
# (desc, addr_1, addr_2, addr_3, cc,
#  exp_town, exp_geonames_id, exp_confidence)
FLOW_1_RESOLVED_SAMPLES = [
    ("Italy Bari Sardo postal-assisted",
     "Localita' Sa Mesa Longa 13", "08042 Bari Sardo (NU)", "Italy", "IT",
     "Bari Sardo", 2525595, CONFIDENCE_EXACT_PRIMARY),

    ("Thailand Krabi single-line",
     "Villa E5, Malee Beach, 541/2 Moo 2 Long Beach Pra-Ae Beach 81150 Krabi, Thailand",
     "", "", "TH",
     "Krabi", 1152633, CONFIDENCE_EXACT_PRIMARY),

    ("US Springfield IL postal-disambiguated",
     "123 Main St, Springfield, IL 62701", "", "", "US",
     "Springfield", 4250542, CONFIDENCE_EXACT_PRIMARY),

    ("Pakistan Taxila postal-fallback",
     "Plot 16-B, Punjab Small Industries Estate",
     "Jhang Bahtra Road, Taxila", "", "PK",
     "Taxila", None, CONFIDENCE_EXACT_ALTERNATE),
]

# -- Flow 1 Unresolved: Step 3 does NOT match --
# (desc, addr_1, addr_2, addr_3, cc,
#  exp_output_status, exp_review_reason)
FLOW_1_UNRESOLVED_SAMPLES = [
    ("Gibberish address",
     "123 Main St Xyzzyville Nowhere", "", "", "US",
     "rejected", "no_town_candidate"),
]

# -- Flow 1 Empty Address --
# (desc, addr_1, addr_2, addr_3, cc)
FLOW_1_EMPTY_SAMPLES = [
    ("All fields blank",     "", "", "", "US"),
    ("Whitespace only",      "   ", "  ", " ", "GB"),
]

# -- Flow 1 Edge Cases: mismatch resolved via suggested CC --
# When Step 1 detects a mismatch (address text says Country X but CC=Y),
# Step 3 now tries the suggested_country_code FIRST, resolving correctly.
# (desc, addr_1, addr_2, addr_3, cc,
#  exp_town, exp_geonames_id, exp_confidence)
FLOW_1_EDGE_SAMPLES = [
    ("Mismatch CC resolved via suggested country",
     "Villa E5, Malee Beach, 541/2 Moo 2 Long Beach Pra-Ae Beach 81150 Krabi, Thailand",
     "", "", "US",
     "Krabi", 1152633, CONFIDENCE_EXACT_PRIMARY),

    ("London with wrong CC resolved via suggested country",
     "10 Downing Street, London SW1A 2AA, United Kingdom", "", "", "AU",
     "London", 2643743, CONFIDENCE_EXACT_PRIMARY),
]

# -- Expected keys in final_result dict --
EXPECTED_FINAL_RESULT_KEYS = {
    "input_address_1", "input_address_2", "input_address_3", "country_code",
    "address_line_1", "address_line_2",
    "town", "country", "street", "building", "postal_code",
    "status", "confidence_score", "parser_source",
    "geonames_match", "geonames_id", "normalized_town",
    "warnings", "review_reason",
    "mismatch_detected", "suggested_country_code",
    "llm_calls", "llm_prompt_tokens", "llm_completion_tokens",
}


# ======================================================================
# HELPERS
# ======================================================================

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "database" / "geonames.db"

_SKIP_NO_DB = pytest.mark.skipif(
    not _DB_PATH.exists(), reason=f"GeoNames DB not found at {_DB_PATH}"
)


def _run_flow_1(address_1, address_2, address_3, country_code):
    """Run Flow 1: Steps 0→1→2→3→8.

    Mirrors the DeterministicResolverAgent logic for the exact-match path.
    If Step 3 resolves, sets status='resolved' and parser_source='libpostal',
    then maps match_confidence → confidence before calling persist.
    """
    state = {
        "address_1": address_1,
        "address_2": address_2,
        "address_3": address_3,
        "country_code": country_code,
        "status": "pending",
        "row_index": 1,
        "warnings": [],
    }

    # Early exit for empty address (matches orchestrator behaviour)
    has_address = any(
        state.get(f"address_{i}") and str(state.get(f"address_{i}")).strip()
        for i in range(1, 4)
    )
    if not has_address:
        state["status"] = "rejected"
        state["parser_source"] = None
        state["warnings"].append("no_address_data")
        persist(state)
        return state

    preprocess(state)       # Step 0
    parse(state)            # Step 1
    lookup(state)           # Step 2
    match(state)            # Step 3

    # Orchestrator status logic (DeterministicResolverAgent)
    if state.get("exact_match"):
        state["status"] = "resolved"
        state["parser_source"] = "libpostal"

    # Map match_confidence → confidence for persist
    state["confidence"] = state.get("match_confidence", 0.0)

    persist(state)          # Step 8
    return state


# ======================================================================
# 1. Happy Path — address resolves at Step 3
# ======================================================================


@_SKIP_NO_DB
class TestFlow1Resolved:
    """Flow 1 happy path: Step 3 produces an exact match, persist validates."""

    @pytest.mark.parametrize(
        "desc, addr_1, addr_2, addr_3, cc, "
        "exp_town, exp_geonames_id, exp_confidence",
        FLOW_1_RESOLVED_SAMPLES,
        ids=[s[0] for s in FLOW_1_RESOLVED_SAMPLES],
    )
    def test_flow_1_resolved(
        self, desc, addr_1, addr_2, addr_3, cc,
        exp_town, exp_geonames_id, exp_confidence,
    ):
        state = _run_flow_1(addr_1, addr_2, addr_3, cc)
        fr = state["final_result"]

        report(f"Flow 1 resolved [{desc}]", {
            "town": fr["town"],
            "geonames_id": fr["geonames_id"],
            "confidence_score": fr["confidence_score"],
            "status": fr["status"],
            "parser_source": fr["parser_source"],
        })

        assert fr["status"] == "validated"
        assert fr["town"] == exp_town
        assert fr["geonames_id"] == exp_geonames_id
        assert fr["confidence_score"] == exp_confidence
        assert fr["geonames_match"] is True
        assert fr["parser_source"] == "libpostal"
        assert fr["review_reason"] is None
        assert fr["llm_calls"] == 0
        assert fr["llm_prompt_tokens"] == 0
        assert fr["llm_completion_tokens"] == 0


# ======================================================================
# 2. Negative — Step 3 does NOT resolve
# ======================================================================


@_SKIP_NO_DB
class TestFlow1Unresolved:
    """Flow 1 negative: Step 3 finds no match, persist rejects."""

    @pytest.mark.parametrize(
        "desc, addr_1, addr_2, addr_3, cc, "
        "exp_output_status, exp_review_reason",
        FLOW_1_UNRESOLVED_SAMPLES,
        ids=[s[0] for s in FLOW_1_UNRESOLVED_SAMPLES],
    )
    def test_flow_1_unresolved(
        self, desc, addr_1, addr_2, addr_3, cc,
        exp_output_status, exp_review_reason,
    ):
        state = _run_flow_1(addr_1, addr_2, addr_3, cc)
        fr = state["final_result"]

        report(f"Flow 1 unresolved [{desc}]", {
            "status": fr["status"],
            "confidence_score": fr["confidence_score"],
            "review_reason": fr["review_reason"],
            "geonames_match": fr["geonames_match"],
        })

        assert fr["status"] == exp_output_status
        assert fr["review_reason"] == exp_review_reason
        assert fr["confidence_score"] == 0.0
        assert fr["geonames_match"] is False
        assert fr["llm_calls"] == 0


# ======================================================================
# 3. Negative — empty / blank address
# ======================================================================


@_SKIP_NO_DB
class TestFlow1EmptyAddress:
    """Flow 1 negative: blank address fields → rejected."""

    @pytest.mark.parametrize(
        "desc, addr_1, addr_2, addr_3, cc",
        FLOW_1_EMPTY_SAMPLES,
        ids=[s[0] for s in FLOW_1_EMPTY_SAMPLES],
    )
    def test_flow_1_empty(self, desc, addr_1, addr_2, addr_3, cc):
        state = _run_flow_1(addr_1, addr_2, addr_3, cc)
        fr = state["final_result"]

        report(f"Flow 1 empty [{desc}]", {
            "status": fr["status"],
            "review_reason": fr["review_reason"],
            "warnings": fr["warnings"],
        })

        assert fr["status"] == "rejected"
        assert fr["review_reason"] == "no_address_data"
        assert fr["confidence_score"] == 0.0
        assert fr["llm_calls"] == 0
        assert "no_address_data" in fr["warnings"]


# ======================================================================
# 4. Edge Cases — alternate name / wrong CC
# ======================================================================


@_SKIP_NO_DB
class TestFlow1EdgeCases:
    """Flow 1 edge cases: alternate-name matches, wrong country codes."""

    @pytest.mark.parametrize(
        "desc, addr_1, addr_2, addr_3, cc, "
        "exp_town, exp_geonames_id, exp_confidence",
        FLOW_1_EDGE_SAMPLES,
        ids=[s[0] for s in FLOW_1_EDGE_SAMPLES],
    )
    def test_flow_1_edge(
        self, desc, addr_1, addr_2, addr_3, cc,
        exp_town, exp_geonames_id, exp_confidence,
    ):
        state = _run_flow_1(addr_1, addr_2, addr_3, cc)
        fr = state["final_result"]

        report(f"Flow 1 edge [{desc}]", {
            "town": fr["town"],
            "geonames_id": fr["geonames_id"],
            "confidence_score": fr["confidence_score"],
            "status": fr["status"],
        })

        # Mismatch resolved: Step 3 tried suggested CC first →
        # correct city found in the right country.
        assert fr["status"] == "validated"
        assert fr["town"] == exp_town
        assert fr["geonames_id"] == exp_geonames_id
        assert fr["confidence_score"] == exp_confidence
        assert fr["geonames_match"] is True
        assert fr["mismatch_detected"] is True
        assert fr["llm_calls"] == 0


# ======================================================================
# 5. Structural — final_result has all expected keys
# ======================================================================


@_SKIP_NO_DB
class TestFlow1FinalResultKeys:
    """Verify final_result dict contains all expected keys."""

    def test_all_keys_present(self):
        addr_1, addr_2, addr_3, cc = (
            FLOW_1_RESOLVED_SAMPLES[0][1],
            FLOW_1_RESOLVED_SAMPLES[0][2],
            FLOW_1_RESOLVED_SAMPLES[0][3],
            FLOW_1_RESOLVED_SAMPLES[0][4],
        )
        state = _run_flow_1(addr_1, addr_2, addr_3, cc)
        fr = state["final_result"]

        report("Flow 1 final_result keys", {"keys": sorted(fr.keys())})

        assert set(fr.keys()) == EXPECTED_FINAL_RESULT_KEYS
