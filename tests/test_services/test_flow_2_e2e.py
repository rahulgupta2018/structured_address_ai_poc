"""End-to-end tests for Flow 2: Steps 0→1→2→3→4→5→8.

Flow 2 is the scanner-resolution path: preprocess → parse → postal lookup →
exact match (no hit) → mismatch detect → address scan → persist. Step 3
does not resolve the city; the scanner (Step 5) catches it via n-gram or
fuzzy matching against the country's GeoNames name set (confidence 0.80).

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
from services.mismatch_detector import detect
from services.address_scanner import scan
from services.persistence import persist
from utils.config import (
    CONFIDENCE_EXACT_PRIMARY,
    CONFIDENCE_FUZZY_SCAN,
)

from tests.test_services.report import report


# ======================================================================
# SAMPLE DATA & EXPECTED RESULTS — edit here, not inside tests
# ======================================================================

# -- Flow 2 Happy Path: Step 3 fails, Step 5 resolves --
# Addresses with unusual format that libpostal cannot parse to a clean
# city name, but the scanner detects the city via n-gram matching.
# (desc, addr_1, addr_2, addr_3, cc,
#  exp_town, exp_geonames_id, exp_confidence)
FLOW_2_RESOLVED_SAMPLES = [
    ("Tokyo freeform industrial",
     "ATTN: SALES DEPT, TOKYO OFFICE, 100-0005 JAPAN", "", "", "JP",
     "Tokyo", 1850147, CONFIDENCE_FUZZY_SCAN),

    ("Mumbai freeform C/O",
     "C/O RAJESH KUMAR, MUMBAI CENTRAL, 400008 INDIA", "", "", "IN",
     "Mumbai", 1275339, CONFIDENCE_FUZZY_SCAN),

    ("Bangkok industrial zone",
     "DISPATCH CENTER, BANGKOK INDUSTRIAL ZONE, 10120 THAILAND", "", "", "TH",
     "Bangkok", 1609350, CONFIDENCE_FUZZY_SCAN),
]

# -- Flow 2 Unresolved: neither Step 3 nor Step 5 matches --
# (desc, addr_1, addr_2, addr_3, cc,
#  exp_output_status, exp_review_reason)
FLOW_2_UNRESOLVED_SAMPLES = [
    ("Gibberish location",
     "123 MAIN STREET NOWHERE SPECIAL", "", "", "US",
     "rejected", "no_town_candidate"),
]

# -- Flow 2 Empty Address --
# (desc, addr_1, addr_2, addr_3, cc)
FLOW_2_EMPTY_SAMPLES = [
    ("All fields blank",     "", "", "", "DE"),
    ("Whitespace only",      "  ", "   ", " ", "FR"),
]

# -- Flow 2 Edge Cases: scanner false positive --
# Previously contained the Taxila/PK case where the scanner matched
# "jhang" from "Jhang Bahtra Road" to city "Jhang Sadr" (false positive).
# Fixed: Step 3 postal-code fallback now resolves Taxila in Flow 1.
# (desc, addr_1, addr_2, addr_3, cc,
#  exp_town, exp_geonames_id, exp_confidence)
FLOW_2_EDGE_SAMPLES = []

# -- Expected keys in final_result dict --
EXPECTED_FINAL_RESULT_KEYS = {
    "address_1", "address_2", "address_3", "country_code",
    "town", "street", "building", "postal_code",
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


def _run_flow_2(address_1, address_2, address_3, country_code):
    """Run Flow 2: Steps 0→1→2→3→4→5→8.

    Mirrors the DeterministicResolverAgent logic. If Step 3 resolves
    (exact match), exits early with Flow 1 behaviour. Otherwise runs
    Steps 4–5 (mismatch detect + scanner) before calling persist.
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

    # Orchestrator: if Step 3 resolved, exit early (Flow 1 behaviour)
    if state.get("exact_match"):
        state["status"] = "resolved"
        state["parser_source"] = "libpostal"
        state["confidence"] = state.get("match_confidence", 0.0)
        persist(state)
        return state

    detect(state)           # Step 4
    scan(state)             # Step 5

    # Orchestrator status logic
    if state.get("scan_match"):
        state["status"] = "resolved"
        state["parser_source"] = "geonames_scan"
        state["town_candidate"] = state.get("scan_candidate")
    else:
        state["status"] = "unresolved"

    # Map match_confidence → confidence for persist
    state["confidence"] = state.get("match_confidence", 0.0)

    persist(state)          # Step 8
    return state


# ======================================================================
# 1. Happy Path — Step 3 fails, Step 5 resolves
# ======================================================================


@_SKIP_NO_DB
class TestFlow2Resolved:
    """Flow 2 happy path: scanner resolves the city, persist validates."""

    @pytest.mark.parametrize(
        "desc, addr_1, addr_2, addr_3, cc, "
        "exp_town, exp_geonames_id, exp_confidence",
        FLOW_2_RESOLVED_SAMPLES,
        ids=[s[0] for s in FLOW_2_RESOLVED_SAMPLES],
    )
    def test_flow_2_resolved(
        self, desc, addr_1, addr_2, addr_3, cc,
        exp_town, exp_geonames_id, exp_confidence,
    ):
        state = _run_flow_2(addr_1, addr_2, addr_3, cc)
        fr = state["final_result"]

        report(f"Flow 2 resolved [{desc}]", {
            "town": fr["town"],
            "geonames_id": fr["geonames_id"],
            "confidence_score": fr["confidence_score"],
            "status": fr["status"],
            "parser_source": fr["parser_source"],
            "exact_match_step3": state.get("exact_match"),
            "scan_match_step5": state.get("scan_match"),
        })

        # Step 3 should NOT have resolved (this is Flow 2)
        assert state.get("exact_match") is not True, (
            f"Step 3 resolved — this is Flow 1, not Flow 2: town={fr['town']}"
        )

        assert fr["status"] == "validated"
        assert fr["town"] == exp_town
        assert fr["geonames_id"] == exp_geonames_id
        assert fr["confidence_score"] == exp_confidence
        assert fr["geonames_match"] is True
        assert fr["parser_source"] == "geonames_scan"
        assert fr["review_reason"] is None
        assert fr["llm_calls"] == 0
        assert fr["llm_prompt_tokens"] == 0
        assert fr["llm_completion_tokens"] == 0


# ======================================================================
# 2. Negative — neither Step 3 nor Step 5 resolves
# ======================================================================


@_SKIP_NO_DB
class TestFlow2Unresolved:
    """Flow 2 negative: no match at Step 3 or Step 5, persist rejects."""

    @pytest.mark.parametrize(
        "desc, addr_1, addr_2, addr_3, cc, "
        "exp_output_status, exp_review_reason",
        FLOW_2_UNRESOLVED_SAMPLES,
        ids=[s[0] for s in FLOW_2_UNRESOLVED_SAMPLES],
    )
    def test_flow_2_unresolved(
        self, desc, addr_1, addr_2, addr_3, cc,
        exp_output_status, exp_review_reason,
    ):
        state = _run_flow_2(addr_1, addr_2, addr_3, cc)
        fr = state["final_result"]

        report(f"Flow 2 unresolved [{desc}]", {
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
class TestFlow2EmptyAddress:
    """Flow 2 negative: blank address fields → rejected."""

    @pytest.mark.parametrize(
        "desc, addr_1, addr_2, addr_3, cc",
        FLOW_2_EMPTY_SAMPLES,
        ids=[s[0] for s in FLOW_2_EMPTY_SAMPLES],
    )
    def test_flow_2_empty(self, desc, addr_1, addr_2, addr_3, cc):
        state = _run_flow_2(addr_1, addr_2, addr_3, cc)
        fr = state["final_result"]

        report(f"Flow 2 empty [{desc}]", {
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
# 4. Edge Cases — scanner false positive
# ======================================================================


@_SKIP_NO_DB
class TestFlow2EdgeCases:
    """Flow 2 edge: scanner false positive (road name matched as city)."""

    @pytest.mark.parametrize(
        "desc, addr_1, addr_2, addr_3, cc, "
        "exp_town, exp_geonames_id, exp_confidence",
        FLOW_2_EDGE_SAMPLES,
        ids=[s[0] for s in FLOW_2_EDGE_SAMPLES],
    )
    def test_flow_2_edge(
        self, desc, addr_1, addr_2, addr_3, cc,
        exp_town, exp_geonames_id, exp_confidence,
    ):
        state = _run_flow_2(addr_1, addr_2, addr_3, cc)
        fr = state["final_result"]

        report(f"Flow 2 edge [{desc}]", {
            "town": fr["town"],
            "geonames_id": fr["geonames_id"],
            "confidence_score": fr["confidence_score"],
            "status": fr["status"],
            "scan_candidate": state.get("scan_candidate"),
        })

        # Scanner matches — pipeline treats it as resolved even if
        # the match is a false positive (documented limitation).
        assert fr["status"] == "validated"
        assert fr["town"] == exp_town
        assert fr["geonames_id"] == exp_geonames_id
        assert fr["confidence_score"] == exp_confidence
        assert fr["geonames_match"] is True
        assert fr["llm_calls"] == 0


# ======================================================================
# 5. Structural — final_result has all expected keys
# ======================================================================


@_SKIP_NO_DB
class TestFlow2FinalResultKeys:
    """Verify final_result dict contains all expected keys."""

    def test_all_keys_present(self):
        addr_1, addr_2, addr_3, cc = (
            FLOW_2_RESOLVED_SAMPLES[0][1],
            FLOW_2_RESOLVED_SAMPLES[0][2],
            FLOW_2_RESOLVED_SAMPLES[0][3],
            FLOW_2_RESOLVED_SAMPLES[0][4],
        )
        state = _run_flow_2(addr_1, addr_2, addr_3, cc)
        fr = state["final_result"]

        report("Flow 2 final_result keys", {"keys": sorted(fr.keys())})

        assert set(fr.keys()) == EXPECTED_FINAL_RESULT_KEYS
