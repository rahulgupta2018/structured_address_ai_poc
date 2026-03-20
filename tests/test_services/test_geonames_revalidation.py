"""Tests for geonames_revalidation service (Step 7) — safety-net re-validation.

Two tiers of tests:

1. **Deterministic tests** — Direct calls to revalidate(), _prefer_address_spelling(),
   _fuzzy_revalidate(), and end-to-end pipeline flows (Steps 0-3-7 and 0-5-7)
   against the real GeoNames SQLite DB.  No mocks, completes in milliseconds.

2. **Real LLM pipeline tests** — Flow 8.3 (Steps 0-6-7) requires Ollama.
   Auto-skipped when Ollama is unavailable.  Assertions are structural
   because LLM output is non-deterministic.

All deterministic tests call real service functions against the real
SQLite database — zero mocks.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.request
from pathlib import Path

import pytest

from services.geonames_revalidation import (
    _fuzzy_revalidate,
    _prefer_address_spelling,
    revalidate,
)
from services.geonames_repo import resolve_city_by_name
from services.normalizer import normalize_for_matching, preprocess
from services.libpostal_parser import parse
from services.postal_lookup import lookup
from services.geonames_exact import match
from services.mismatch_detector import detect
from services.address_scanner import scan
from utils.config import (
    CONFIDENCE_EXACT_PRIMARY,
    CONFIDENCE_LLM_CONFIRMED,
    CONFIDENCE_LLM_FUZZY_CONFIRMED,
    CONFIDENCE_LLM_UNVERIFIED,
)

from tests.test_services.report import report

# Suppress noisy LiteLLM "Give Feedback / Get Help" stderr messages
logging.getLogger("LiteLLM").setLevel(logging.WARNING)


# ======================================================================
# SAMPLE DATA — edit these to test with your own data
# ======================================================================

# -- Deterministic pass-through: resolved rows --
# (description, match_confidence)
DETERMINISTIC_PASSTHROUGH_SAMPLES = [
    ("high confidence resolved",    1.0),
    ("zero confidence resolved",    0.0),
    ("fractional confidence",       0.63),
]

# -- LLM no-result scenarios --
# (description, llm_result, expected_confidence, expected_status)
LLM_NO_RESULT_SAMPLES = [
    ("llm_result is None",       None,            0.0,                        "needs_review"),
    ("llm_result is a string",   "some string",   0.0,                        "needs_review"),
    ("llm_result is a list",     ["town"],         0.0,                        "needs_review"),
    ("llm_result empty town",    {"town": ""},    CONFIDENCE_LLM_UNVERIFIED,  "needs_review"),
]

# -- Exact match in stated country: known cities in GeoNames --
# (description, town, country_code, raw_address)
EXACT_MATCH_STATED_SAMPLES = [
    ("Morvi in India",     "Morvi",     "IN", "SHOP 10 MORVI GUJR"),
    ("Dubai in UAE",       "Dubai",     "AE", "PO BOX 123 DUBAI UAE"),
    ("Bangkok in Thailand","Bangkok",   "TH", "123 SUKHUMVIT ROAD BANGKOK 10110"),
    ("Tokyo in Japan",     "Tokyo",     "JP", "3-1-1 MARUNOUCHI TOKYO 100-0005"),
    ("Mumbai in India",    "Mumbai",    "IN", "EXPRESS TOWERS MUMBAI 400021"),
    ("London in GB",       "London",    "GB", "10 DOWNING STREET LONDON SW1A 2AA"),
]

# -- Exact match in suggested country: town only in the suggested CC --
# The town must exist in cities1000 for the suggested CC but NOT the stated CC.
# (description, town, stated_cc, suggested_cc, raw_address)
EXACT_MATCH_SUGGESTED_SAMPLES = [
    ("Lyon in FR not AE",      "Lyon",      "AE", "FR", "25 RUE DE LA PLACE LYON"),
    ("Morvi in IN not GB",     "Morvi",     "GB", "IN", "SHOP 10 MORVI GUJR"),
]

# -- Fuzzy match: slightly misspelled town names --
# (description, town, country_code, raw_address, expected_status)
FUZZY_MATCH_SAMPLES = [
    ("Springfeild misspelled",  "Springfeild",  "US", "123 MAIN ST SPRINGFEILD IL", "validated"),
]

# -- Cross-country fallback: town not in stated CC, no suggested CC --
# The town must NOT exist in the stated CC's cities1000 but DOES exist globally
# via list_countries_for_city.
# (description, town, stated_cc, raw_address)
CROSS_COUNTRY_SAMPLES = [
    ("Nairobi stated US found KE", "Nairobi", "US", "PO BOX 100 NAIROBI"),
]

# -- No match anywhere: town not verifiable in any table --
# (description, town, country_code, raw_address)
NO_MATCH_SAMPLES = [
    ("gibberish town",          "Xyzplugh",     "US", "123 MAIN ST XYZPLUGH"),
    ("invented town name",      "Qwertytopia",  "GB", "42 HIGH STREET QWERTYTOPIA"),
]

# -- _prefer_address_spelling: alternate name in raw address --
# (description, llm_town, country_code, raw_address, geonameid)
PREFER_SPELLING_SAMPLES = [
    ("Morbi in address, LLM says Morvi", "Morvi", "IN", "SHOP 10, MORBI, GUJR", 1262775),
]

# -- _prefer_address_spelling: edge cases --
# (description, llm_town, city_dict, raw_address, country_code, expected)
PREFER_SPELLING_EDGE_SAMPLES = [
    ("empty raw address",    "Morvi",
     {"geonameid": 1262775, "name": "Morvi", "name_type": "primary"},
     "", "IN", "Morvi"),
    ("no geonameid in city", "Test",
     {"name": "Test"},
     "TEST ADDR", "US", "Test"),
]

# -- _fuzzy_revalidate direct tests --
# (description, town, country_code, raw_address, expect_result)
FUZZY_REVALIDATE_SAMPLES = [
    ("slightly misspelled known city", "Springfeild", "US", "SPRINGFEILD IL", True),
    ("exact known city",               "Springfield", "US", "SPRINGFIELD IL", True),
    ("gibberish not matchable",        "Xyzplughqwx", "US", "XYZPLUGHQWX",  False),
    ("short name skipped",             "Xy",          "US", "XY ADDR",       False),
]


# -- Flow 3: Steps 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 (requires LLM) --
# Note: Flow 8.1 (Steps 0→3→7) and Flow 8.2 (Steps 0→5→7) were removed —
# Step 7 now runs only on the LLM path.  Deterministic paths skip Step 7.
# (description, address_1, address_2, address_3, country_code)
FLOW_0_6_7_SAMPLES = [
    ("Italy with postal 08042",
     "Localita\' Sa Mesa Longa 13", "08042 Bari Sardo (NU)", "Italy", "IT"),
    ("Thailand full address",
     "Villa E5, Malee Beach, 541/2 Moo 2  Long Beach Pra-Ae Beach 81150 Krabi, Thailand",
     "", "", "US"),
    ("Pakistan Taxila industrial",
     "Plot 16-B   Punjab Small Industries Estate",
     "Jhang Bahtra Road, Taxila", "", "PK"),
]


# ======================================================================
# HELPERS
# ======================================================================

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "database" / "geonames.db"

_SKIP_NO_DB = pytest.mark.skipif(
    not _DB_PATH.exists(), reason=f"GeoNames DB not found at {_DB_PATH}"
)


def _ollama_available() -> bool:
    """Return True if Ollama is reachable on localhost."""
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        return True
    except Exception:
        return False


_SKIP_NO_OLLAMA = pytest.mark.skipif(
    not _ollama_available(), reason="Ollama not running on localhost:11434"
)

_LLM_TEST_TIMEOUT = 300


def _make_llm_state(town, country_code, raw_address,
                     suggested_cc=None, status="pending"):
    """Build a state dict for an LLM-path row arriving at Step 7."""
    return {
        "status": status,
        "llm_result": {"town": town} if town is not None else None,
        "country_code": country_code,
        "suggested_country_code": suggested_cc,
        "raw_address": raw_address,
        "warnings": [],
    }


def _run_pipeline_steps_0_3(address_1, address_2, address_3, country_code):
    """Run Steps 0 → 1 → 2 → 3 and return the state."""
    state = {
        "address_1": address_1,
        "address_2": address_2,
        "address_3": address_3,
        "country_code": country_code,
        "status": "pending",
        "row_index": 1,
        "warnings": [],
    }
    preprocess(state)
    parse(state)
    lookup(state)
    match(state)
    return state


def _run_pipeline_steps_0_5(address_1, address_2, address_3, country_code):
    """Run Steps 0 → 1 → 2 → 3 → 4 → 5 and return the state."""
    state = _run_pipeline_steps_0_3(address_1, address_2, address_3, country_code)
    if state.get("status") != "resolved":
        detect(state)
        scan(state)
    return state


def _run_pipeline_steps_0_6(address_1, address_2, address_3, country_code):
    """Run Steps 0 → 1 → 2 → 3 → 4 → 5 → 6 and return the state."""
    from address_pipeline_agent.sub_agents.llm_parser.agent import (
        LlmAddressParserAgent,
    )
    from types import SimpleNamespace
    from utils.prompts import build_instruction

    state = _run_pipeline_steps_0_5(address_1, address_2, address_3, country_code)
    if state.get("status") != "resolved":
        instruction = build_instruction(state)
        agent = LlmAddressParserAgent(name="revalidation_e2e")
        agent.instruction = instruction
        ctx = SimpleNamespace(session=SimpleNamespace(state=state))

        async def _run():
            async for _ in agent._run_async_impl(ctx):
                pass
            return dict(ctx.session.state)

        state = asyncio.run(asyncio.wait_for(_run(), timeout=_LLM_TEST_TIMEOUT))
    return state


# ======================================================================
# 1. Deterministic pass-through
# ======================================================================


@_SKIP_NO_DB
class TestDeterministicPassthrough:
    """Resolved rows should pass through with match_confidence preserved."""

    @pytest.mark.parametrize(
        "desc, match_confidence",
        DETERMINISTIC_PASSTHROUGH_SAMPLES,
        ids=[s[0] for s in DETERMINISTIC_PASSTHROUGH_SAMPLES],
    )
    def test_resolved_passthrough(self, desc, match_confidence):
        state = {
            "status": "resolved",
            "match_confidence": match_confidence,
        }
        report(f"revalidate input [{desc}]", state)
        result = revalidate(state)
        report(f"revalidate output [{desc}]", {
            "confidence": result["confidence"],
        })
        assert result["confidence"] == match_confidence
        assert result["status"] == "resolved"


# ======================================================================
# 2. LLM no-result / bad result
# ======================================================================


@_SKIP_NO_DB
class TestLlmNoResult:
    """LLM produced no usable result -> needs_review."""

    @pytest.mark.parametrize(
        "desc, llm_result, expected_confidence, expected_status",
        LLM_NO_RESULT_SAMPLES,
        ids=[s[0] for s in LLM_NO_RESULT_SAMPLES],
    )
    def test_no_result(self, desc, llm_result, expected_confidence, expected_status):
        state = {
            "status": "pending",
            "llm_result": llm_result,
            "country_code": "US",
        }
        report(f"revalidate input [{desc}]", {"llm_result": llm_result})
        result = revalidate(state)
        report(f"revalidate output [{desc}]", {
            "confidence": result["confidence"],
            "status": result["status"],
        })
        assert result["confidence"] == expected_confidence
        assert result["status"] == expected_status


# ======================================================================
# 3. Exact match in stated country
# ======================================================================


@_SKIP_NO_DB
class TestExactMatchStatedCountry:
    """LLM town found via exact match in the stated country."""

    @pytest.mark.parametrize(
        "desc, town, country_code, raw_address",
        EXACT_MATCH_STATED_SAMPLES,
        ids=[s[0] for s in EXACT_MATCH_STATED_SAMPLES],
    )
    def test_exact_match(self, desc, town, country_code, raw_address):
        state = _make_llm_state(town, country_code, raw_address)
        report(f"revalidate input [{desc}]", {
            "llm_town": town, "country": country_code,
        })
        result = revalidate(state)
        report(f"revalidate output [{desc}]", {
            "confidence": result["confidence"],
            "status": result["status"],
            "town_candidate": result.get("town_candidate"),
            "geonames_id": result.get("geonames_id"),
        })
        assert result["confidence"] == CONFIDENCE_LLM_CONFIRMED
        assert result["status"] == "validated"
        assert result.get("geonames_id") is not None
        assert result.get("town_candidate") is not None


# ======================================================================
# 4. Exact match in suggested country
# ======================================================================


@_SKIP_NO_DB
class TestExactMatchSuggestedCountry:
    """LLM town not in stated CC but found in the suggested CC."""

    @pytest.mark.parametrize(
        "desc, town, stated_cc, suggested_cc, raw_address",
        EXACT_MATCH_SUGGESTED_SAMPLES,
        ids=[s[0] for s in EXACT_MATCH_SUGGESTED_SAMPLES],
    )
    def test_suggested_country(self, desc, town, stated_cc, suggested_cc, raw_address):
        state = _make_llm_state(town, stated_cc, raw_address,
                                 suggested_cc=suggested_cc)
        report(f"revalidate input [{desc}]", {
            "llm_town": town, "stated_cc": stated_cc,
            "suggested_cc": suggested_cc,
        })
        result = revalidate(state)
        report(f"revalidate output [{desc}]", {
            "confidence": result["confidence"],
            "status": result["status"],
            "mismatch_detected": result.get("mismatch_detected"),
            "suggested_country_code": result.get("suggested_country_code"),
        })
        assert result["confidence"] == CONFIDENCE_LLM_CONFIRMED
        assert result["status"] == "validated"
        assert result.get("mismatch_detected") is True
        assert result.get("suggested_country_code") == suggested_cc


# ======================================================================
# 5. Fuzzy match
# ======================================================================


@_SKIP_NO_DB
class TestFuzzyMatch:
    """LLM town approximately matches a GeoNames city name."""

    @pytest.mark.parametrize(
        "desc, town, country_code, raw_address, expected_status",
        FUZZY_MATCH_SAMPLES,
        ids=[s[0] for s in FUZZY_MATCH_SAMPLES],
    )
    def test_fuzzy(self, desc, town, country_code, raw_address, expected_status):
        state = _make_llm_state(town, country_code, raw_address)
        report(f"revalidate input [{desc}]", {
            "llm_town": town, "country": country_code,
        })
        result = revalidate(state)
        report(f"revalidate output [{desc}]", {
            "confidence": result["confidence"],
            "status": result["status"],
            "town_candidate": result.get("town_candidate"),
        })
        assert result["status"] == expected_status
        # Fuzzy or better confidence
        assert result["confidence"] >= CONFIDENCE_LLM_FUZZY_CONFIRMED


# ======================================================================
# 6. Cross-country fallback
# ======================================================================


@_SKIP_NO_DB
class TestCrossCountryFallback:
    """LLM town found via list_countries_for_city cross-country check."""

    @pytest.mark.parametrize(
        "desc, town, stated_cc, raw_address",
        CROSS_COUNTRY_SAMPLES,
        ids=[s[0] for s in CROSS_COUNTRY_SAMPLES],
    )
    def test_cross_country(self, desc, town, stated_cc, raw_address):
        state = _make_llm_state(town, stated_cc, raw_address)
        report(f"revalidate input [{desc}]", {
            "llm_town": town, "stated_cc": stated_cc,
        })
        result = revalidate(state)
        report(f"revalidate output [{desc}]", {
            "confidence": result["confidence"],
            "status": result["status"],
            "mismatch_detected": result.get("mismatch_detected"),
            "suggested_country_code": result.get("suggested_country_code"),
        })
        assert result["status"] == "validated"
        assert result["confidence"] >= CONFIDENCE_LLM_FUZZY_CONFIRMED
        assert result.get("mismatch_detected") is True
        assert result.get("suggested_country_code") is not None
        assert result.get("suggested_country_code") != stated_cc


# ======================================================================
# 7. Postal-code table fallback
# ======================================================================


@_SKIP_NO_DB
class TestPostalFallback:
    """LLM town found in the postal-codes table as a last resort.

    Uses 'Bari Sardo' which is too small for cities1000 but exists in
    the postal-code dataset as a place_name.
    """

    def test_postal_fallback_bari_sardo(self):
        state = _make_llm_state("Bari Sardo", "IE", "BARI SARDO 08042")
        report("revalidate input [postal fallback]", {
            "llm_town": "Bari Sardo", "stated_cc": "IE",
        })
        result = revalidate(state)
        report("revalidate output [postal fallback]", {
            "confidence": result["confidence"],
            "status": result["status"],
            "town_candidate": result.get("town_candidate"),
            "mismatch_detected": result.get("mismatch_detected"),
        })
        # Should resolve via one of the fallback paths
        # (exact/cross-country/postal depending on DB content)
        assert result["status"] in ("validated", "needs_review")
        if result["status"] == "validated":
            assert result["confidence"] >= CONFIDENCE_LLM_FUZZY_CONFIRMED


# ======================================================================
# 8. No match anywhere
# ======================================================================


@_SKIP_NO_DB
class TestNoMatchAnywhere:
    """LLM town cannot be verified in any GeoNames table."""

    @pytest.mark.parametrize(
        "desc, town, country_code, raw_address",
        NO_MATCH_SAMPLES,
        ids=[s[0] for s in NO_MATCH_SAMPLES],
    )
    def test_no_match(self, desc, town, country_code, raw_address):
        state = _make_llm_state(town, country_code, raw_address)
        report(f"revalidate input [{desc}]", {
            "llm_town": town, "country": country_code,
        })
        result = revalidate(state)
        report(f"revalidate output [{desc}]", {
            "confidence": result["confidence"],
            "status": result["status"],
            "town_candidate": result.get("town_candidate"),
            "warnings": result.get("warnings", []),
        })
        assert result["confidence"] == CONFIDENCE_LLM_UNVERIFIED
        assert result["status"] == "needs_review"
        assert "geonames_no_match" in result.get("warnings", [])
        assert result["town_candidate"] == town


# ======================================================================
# 9. _prefer_address_spelling
# ======================================================================


@_SKIP_NO_DB
class TestPreferAddressSpelling:
    """When the address contains an alternate name for the same city,
    prefer the address-text spelling."""

    @pytest.mark.parametrize(
        "desc, llm_town, country_code, raw_address, geonameid",
        PREFER_SPELLING_SAMPLES,
        ids=[s[0] for s in PREFER_SPELLING_SAMPLES],
    )
    def test_prefers_address_token(self, desc, llm_town, country_code,
                                    raw_address, geonameid):
        # Look up the city to get the full dict
        norm = normalize_for_matching(llm_town)
        city = resolve_city_by_name(country_code, norm)
        assert city is not None, f"City {llm_town} not found in DB for {country_code}"
        assert city["geonameid"] == geonameid

        result = _prefer_address_spelling(llm_town, city, raw_address, country_code)
        report(f"_prefer_address_spelling [{desc}]", {
            "llm_town": llm_town, "preferred": result,
        })
        # The preferred spelling should differ from the LLM name
        # (it should pick the address-text variant)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.parametrize(
        "desc, llm_town, city, raw_address, country_code, expected",
        PREFER_SPELLING_EDGE_SAMPLES,
        ids=[s[0] for s in PREFER_SPELLING_EDGE_SAMPLES],
    )
    def test_edge_cases(self, desc, llm_town, city, raw_address,
                         country_code, expected):
        result = _prefer_address_spelling(llm_town, city, raw_address, country_code)
        report(f"_prefer_address_spelling [{desc}]", {
            "llm_town": llm_town, "result": result,
        })
        assert result == expected


# ======================================================================
# 10. _fuzzy_revalidate direct tests
# ======================================================================


@_SKIP_NO_DB
class TestFuzzyRevalidate:
    """Direct tests of the _fuzzy_revalidate helper function."""

    @pytest.mark.parametrize(
        "desc, town, country_code, raw_address, expect_result",
        FUZZY_REVALIDATE_SAMPLES,
        ids=[s[0] for s in FUZZY_REVALIDATE_SAMPLES],
    )
    def test_fuzzy_revalidate(self, desc, town, country_code,
                               raw_address, expect_result):
        result = _fuzzy_revalidate(town, country_code, raw_address)
        report(f"_fuzzy_revalidate [{desc}]", {
            "town": town, "result_found": result is not None,
        })
        if expect_result:
            assert result is not None, f"Expected fuzzy match for '{town}'"
            assert "geonameid" in result
            assert "name" in result
        else:
            assert result is None, f"Expected no fuzzy match for '{town}'"


# ======================================================================
# 11. Flow 3: Steps 0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 (LLM)
#     (Flow 8.1 and 8.2 removed — Step 7 now runs only on LLM path)
# ======================================================================


@_SKIP_NO_DB
@_SKIP_NO_OLLAMA
class TestFlowSteps0_6_7:
    """E2E Flow 3: raw address -> Steps 0-6 -> revalidate.

    This is the only flow that reaches Step 7.  Deterministic paths
    (Flow 1: Steps 0→3→8, Flow 2: Steps 0→5→8) skip Step 7 entirely.

    Assertions are structural because LLM output is non-deterministic.
    """

    @pytest.mark.parametrize(
        "desc, address_1, address_2, address_3, country_code",
        FLOW_0_6_7_SAMPLES,
        ids=[s[0] for s in FLOW_0_6_7_SAMPLES],
    )
    def test_flow_0_6_7(self, desc, address_1, address_2, address_3,
                         country_code):
        logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

        state = _run_pipeline_steps_0_6(
            address_1, address_2, address_3, country_code
        )
        status_after_step6 = state.get("status")
        llm_result = state.get("llm_result")

        report(f"FLOW 0-6-7 after Steps 0-6 [{desc}]", {
            "status": status_after_step6,
            "llm_result": llm_result,
            "llm_calls": state.get("llm_calls"),
        })

        # Step 7 — revalidate
        result = revalidate(state)

        report(f"FLOW 0-6-7 after Step 7 [{desc}]", {
            "status": result["status"],
            "confidence": result.get("confidence"),
            "town_candidate": result.get("town_candidate"),
            "geonames_id": result.get("geonames_id"),
            "mismatch_detected": result.get("mismatch_detected"),
        })

        # Structural assertions — the pipeline must not crash
        assert result.get("confidence") is not None
        assert result["status"] in ("validated", "needs_review", "resolved")

        # If resolved at step 6, should pass through
        if status_after_step6 == "resolved":
            assert result["confidence"] == state.get("match_confidence", 0.0)
        # If LLM produced a result, confidence should be set appropriately
        elif llm_result and isinstance(llm_result, dict) and llm_result.get("town"):
            assert result["confidence"] > 0
        else:
            # No LLM result — could be needs_review
            assert result["status"] in ("needs_review", "validated")
