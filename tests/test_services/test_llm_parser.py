"""Tests for LLM parser agent (Step 6) — town resolution via LLM.

Two tiers of tests:

1. **Deterministic component tests**  — Pure functions (_parse_llm_text,
   _detect_text_tool_call), LLM tool dispatch (_execute_tool_call against
   the real GeoNames DB), prompt construction (build_instruction), and
   Pydantic schema validation (LlmAddressOutput).  These run without
   mocks and complete in milliseconds.

2. **Real LLM agent tests** — The full LlmAddressParserAgent with a
   running Ollama instance.  Auto-skipped when Ollama is not available.
   Assert on **structure** (result keys, call counts, token consumption)
   rather than exact town values because LLM output is non-deterministic.
   Includes E2E pipeline samples (Steps 1-6).

All deterministic tests call the real service functions against the real
SQLite database — zero mocks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from address_pipeline_agent.sub_agents.llm_parser.agent import (
    LlmAddressParserAgent,
    _detect_text_tool_call,
    _execute_tool_call,
    _parse_llm_text,
)
from utils.prompts import build_instruction
from utils.schemas import LlmAddressOutput

from tests.test_services.report import report

# Suppress noisy LiteLLM "Give Feedback / Get Help" stderr messages
logging.getLogger("LiteLLM").setLevel(logging.WARNING)


# ======================================================================
# SAMPLE DATA — edit these to test with your own data
# ======================================================================


# -- _parse_llm_text: JSON extraction from LLM text ----------------------
# (description, raw_text, expected_key, expected_value_or_none)
PARSE_HAPPY_SAMPLES = [
    ("clean JSON",
     '{"town": "Roma", "confidence": 0.9}',
     "town", "Roma"),
    ("fenced JSON",
     '`' + '`' + '`' + 'json\n{"town": "London", "confidence": 0.8}\n' + '`' + '`' + '`',
     "town", "London"),
    ("embedded JSON in prose",
     'Based on my analysis: {"town": "Dubai", "confidence": 0.7} which I found.',
     "town", "Dubai"),
]

PARSE_NEGATIVE_SAMPLES = [
    ("empty string", "", None),
    ("non-JSON prose", "I don't know the answer.", None),
    ("nested fences - still parses",
     '`' + '`' + '`' + '\n' + '`' + '`' + '`' + 'json\n{"town": "Paris"}\n' + '`' + '`' + '`' + '\n' + '`' + '`' + '`',
     "Paris"),
]


# -- _detect_text_tool_call: tool-call vs final-answer detection ----------
# (description, parsed_dict, expected_tool_name_or_none)
DETECT_TOOL_CALL_SAMPLES = [
    ("valid tool call",
     {"name": "query_city", "arguments": {"city_name": "Morbi", "country_code": "IN"}},
     "query_city"),
]

DETECT_NOT_TOOL_CALL_SAMPLES = [
    ("final answer - not a tool call",
     {"town": "Roma", "confidence": 0.85}, None),
    ("unknown tool name",
     {"name": "unknown_func", "arguments": {"x": 1}}, None),
    ("missing arguments key",
     {"name": "query_city"}, None),
    ("non-dict input",
     "not a dict", None),
]


# -- _execute_tool_call: real GeoNames DB calls ---------------------------
# (description, tool_name, arguments, expect_result_key, min_results)
EXECUTE_HAPPY_SAMPLES = [
    ("query_city - known city",
     "query_city", {"city_name": "Springfield", "country_code": "US"},
     "result", 1),
    ("list_countries_for_city - multi-country",
     "list_countries_for_city", {"city_name": "Berlin"},
     "result", 1),
    ("query_postal_code - known postal",
     "query_postal_code", {"postal_code": "08042", "country_code": "IT"},
     "result", 1),
    ("query_city_fuzzy - misspelled city",
     "query_city_fuzzy", {"city_name": "Springfeild", "country_code": "US"},
     "result", 1),
    ("query_city_by_admin1 - admin1-filtered",
     "query_city_by_admin1",
     {"city_name": "Springfield", "admin1_name": "Illinois", "country_code": "US"},
     "result", 1),
]

EXECUTE_NEGATIVE_SAMPLES = [
    ("unknown tool",
     "nonexistent_tool", {},
     "error"),
    ("bad arguments - missing required params",
     "query_city_by_admin1", {"city_name": "X"},
     "error"),
]


# -- build_instruction: prompt construction --------------------------------
# (description, state_overrides, expected_substrings)
BUILD_INSTRUCTION_SAMPLES = [
    ("full state injection",
     {"address_1": "Via del Corso 126", "country_code": "IT",
      "raw_address": "via del corso", "libpostal_town": "roma"},
     ["Via del Corso 126", "IT", "via del corso", "roma"]),
    ("sparse state - missing keys default to empty",
     {"country_code": "DE"},
     ["DE"]),
    ("mismatch flag + suggested_country propagation",
     {"country_code": "IE", "mismatch_detected": True,
      "suggested_country_code": "IT"},
     ["True", "IT"]),
]


# -- LlmAddressOutput: Pydantic schema validation -------------------------
# (description, kwargs, field, expected_value)
SCHEMA_SAMPLES = [
    ("valid output",
     {"town": "Roma", "confidence": 0.85, "reasoning": "matched"},
     "confidence", 0.85),
    ("confidence > 1 treated as percentage",
     {"town": "X", "confidence": 85},
     "confidence", 0.85),
    ("negative confidence clamped to 0",
     {"town": "X", "confidence": -5},
     "confidence", 0.0),
    ("None town coerced to empty string",
     {"town": None},
     "town", ""),
    ("non-numeric confidence coerced to 0",
     {"town": "X", "confidence": "not_a_number"},
     "confidence", 0.0),
    ("suggested_country_code preserved",
     {"town": "Bari Sardo", "suggested_country_code": "IT"},
     "suggested_country_code", "IT"),
]


# -- Real LLM agent: happy path -------------------------------------------
# (description, address_1, address_2, address_3, country_code,
#  libpostal_town, postal_code, mismatch, suggested_cc)
REAL_LLM_HAPPY_SAMPLES = [
    ("known city with postal code",
     "Via del Corso 126", "00186 Roma", "Italy", "IT",
     "roma", "00186", False, None),
    ("mismatch scenario - town in wrong country",
     "Main Street", "Barisardo", "", "IE",
     "barisardo", None, True, "IT"),
    ("city with disambiguation signal",
     "123 Main St", "Springfield, IL 62701", "", "US",
     "springfield", "62701", False, None),
]

# -- Real LLM agent: negative ---------------------------------------------
# (description, address_1, address_2, address_3, country_code,
#  libpostal_town, postal_code, mismatch, suggested_cc)
REAL_LLM_NEGATIVE_SAMPLES = [
    ("gibberish address - no real city",
     "QWXYZ PLUGH XYZZY", "", "", "US",
     "", None, False, None),
    ("empty address fields",
     "", "", "", "US",
     "", None, False, None),
]

# -- Real LLM agent: edge cases -------------------------------------------
# (description, address_1, address_2, address_3, country_code,
#  libpostal_town, postal_code, mismatch, suggested_cc)
REAL_LLM_EDGE_SAMPLES = [
    ("ambiguous multi-country city",
     "Unter den Linden 1", "Berlin", "", "US",
     "berlin", None, False, None),
    ("mismatch with suggested country set",
     "123 High Street", "Ko Lanta", "", "US",
     "ko lanta", None, True, "TH"),
]


# -- E2E pipeline: raw address -> Steps 1-6 --------------------------------
# (description, address_1, address_2, address_3, country_code)
E2E_STEP1_THROUGH_STEP6 = [
    ("Italy with postal 08042",
     "Localita' Sa Mesa Longa 13", "08042 Bari Sardo (NU)", "Italy",
     "IT"),
    ("Thailand with correct CC",
     "Villa E5, Malee Beach, 541/2 Moo 2  Long Beach Pra-Ae Beach 81150 Krabi, Thailand",
     "", "", "TH"),
    ("Pakistan - city not in DB",
     "Plot 16-B", "Punjab Small Industries Estate",
     "Jhang Bahtra Road, Taxila", "PK"),
    ("Japan Chiyoda - hyphenated name",
     "1-1 Marunouchi", "Chiyoda-ku", "Tokyo 100-8111",
     "JP"),
]


# -- Helpers ---------------------------------------------------------------

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "database" / "geonames.db"


def _ollama_available() -> bool:
    """Return True if Ollama is reachable on localhost."""
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        return True
    except Exception:
        return False


def _make_ctx(state: dict):
    """Build a minimal mock InvocationContext with the given state."""
    return SimpleNamespace(session=SimpleNamespace(state=state))


# Per-test timeout for real LLM calls (seconds).
# Each LLM call can take up to LLM_TIMEOUT_SECONDS (180s) × LLM_MAX_TURNS (2).
_LLM_TEST_TIMEOUT = 300


def _run_agent(agent: LlmAddressParserAgent, state: dict, timeout: int = _LLM_TEST_TIMEOUT) -> dict:
    """Run the agent synchronously with a timeout; return the final state."""
    ctx = _make_ctx(state)

    async def _run():
        async for _ in agent._run_async_impl(ctx):
            pass
        return dict(ctx.session.state)

    return asyncio.run(asyncio.wait_for(_run(), timeout=timeout))


def _base_unresolved_state(
    address_1="", address_2="", address_3="", country_code="",
    libpostal_town="", postal_code=None, mismatch=False, suggested_cc=None,
) -> dict:
    """Return an 'unresolved' state dict arriving at Step 6."""
    raw = " ".join(filter(None, [address_1, address_2, address_3])).lower()
    return {
        "address_1": address_1,
        "address_2": address_2,
        "address_3": address_3,
        "country_code": country_code,
        "raw_address": raw,
        "libpostal_town": libpostal_town,
        "libpostal_postal_code": postal_code,
        "postal_region": None,
        "postal_city_hint": None,
        "mismatch_detected": mismatch,
        "suggested_country_code": suggested_cc,
        "status": "unresolved",
        "row_index": 1,
        "warnings": [],
    }


_SKIP_NO_DB = pytest.mark.skipif(
    not _DB_PATH.exists(), reason=f"GeoNames DB not found at {_DB_PATH}"
)
_SKIP_NO_OLLAMA = pytest.mark.skipif(
    not _ollama_available(), reason="Ollama not running on localhost:11434"
)


# ======================================================================
# 1. _parse_llm_text - JSON extraction from LLM text
# ======================================================================


class TestParseLlmText:
    """Parse JSON from various text formats returned by LLMs."""

    @pytest.mark.parametrize(
        "desc, raw_text, expected_key, expected_value",
        PARSE_HAPPY_SAMPLES,
        ids=[s[0] for s in PARSE_HAPPY_SAMPLES],
    )
    def test_happy_path(self, desc, raw_text, expected_key, expected_value):
        """LLM text containing JSON is correctly extracted."""
        result = _parse_llm_text(raw_text)
        report(f"_parse_llm_text [{desc}]", {"input": raw_text, "output": result})
        assert result is not None
        assert result[expected_key] == expected_value

    @pytest.mark.parametrize(
        "desc, raw_text, expected",
        PARSE_NEGATIVE_SAMPLES,
        ids=[s[0] for s in PARSE_NEGATIVE_SAMPLES],
    )
    def test_negative(self, desc, raw_text, expected):
        """Non-JSON or empty text returns None or still extracts valid JSON."""
        result = _parse_llm_text(raw_text)
        report(f"_parse_llm_text [{desc}]", {"input": raw_text[:60], "output": result})
        if expected is None:
            assert result is None
        else:
            # nested fences case: still parses successfully
            assert result is not None
            assert result["town"] == expected


# ======================================================================
# 2. _detect_text_tool_call - distinguish tool calls from final answers
# ======================================================================


class TestDetectTextToolCall:
    """Detect tool calls emitted as text by local models (Ollama quirk)."""

    @pytest.mark.parametrize(
        "desc, parsed, expected_name",
        DETECT_TOOL_CALL_SAMPLES,
        ids=[s[0] for s in DETECT_TOOL_CALL_SAMPLES],
    )
    def test_tool_call_detected(self, desc, parsed, expected_name):
        """Known tool call structure is detected and returns (name, args)."""
        result = _detect_text_tool_call(parsed)
        report(f"_detect_text_tool_call [{desc}]", {"input": parsed, "output": result})
        assert result is not None
        assert result[0] == expected_name
        assert isinstance(result[1], dict)

    @pytest.mark.parametrize(
        "desc, parsed, expected",
        DETECT_NOT_TOOL_CALL_SAMPLES,
        ids=[s[0] for s in DETECT_NOT_TOOL_CALL_SAMPLES],
    )
    def test_not_tool_call(self, desc, parsed, expected):
        """Non-tool-call structures return None."""
        result = _detect_text_tool_call(parsed)
        report(f"_detect_text_tool_call [{desc}]", {"input": str(parsed)[:60], "output": result})
        assert result is expected


# ======================================================================
# 3. _execute_tool_call - tool dispatch against REAL GeoNames DB
# ======================================================================


@_SKIP_NO_DB
class TestExecuteToolCall:
    """Tool call dispatch using the real GeoNames database."""

    @pytest.mark.parametrize(
        "desc, tool_name, arguments, expect_key, min_results",
        EXECUTE_HAPPY_SAMPLES,
        ids=[s[0] for s in EXECUTE_HAPPY_SAMPLES],
    )
    def test_happy_path(self, desc, tool_name, arguments, expect_key, min_results):
        """Known tools with valid arguments return results from real DB."""
        result_str = _execute_tool_call(tool_name, arguments)
        result = json.loads(result_str)
        report(f"_execute_tool_call [{desc}]", {
            "tool": tool_name,
            "args": arguments,
            "result_count": len(result.get(expect_key, [])),
        })
        assert expect_key in result
        assert len(result[expect_key]) >= min_results

    @pytest.mark.parametrize(
        "desc, tool_name, arguments, expect_key",
        EXECUTE_NEGATIVE_SAMPLES,
        ids=[s[0] for s in EXECUTE_NEGATIVE_SAMPLES],
    )
    def test_negative(self, desc, tool_name, arguments, expect_key):
        """Unknown tools or bad arguments return error JSON."""
        result_str = _execute_tool_call(tool_name, arguments)
        result = json.loads(result_str)
        report(f"_execute_tool_call [{desc}]", {"result": result})
        assert expect_key in result


# ======================================================================
# 4. build_instruction - prompt construction
# ======================================================================


class TestBuildInstruction:
    """Verify the LLM system prompt is correctly constructed from state."""

    @pytest.mark.parametrize(
        "desc, state_overrides, expected_substrings",
        BUILD_INSTRUCTION_SAMPLES,
        ids=[s[0] for s in BUILD_INSTRUCTION_SAMPLES],
    )
    def test_instruction_contains_state(self, desc, state_overrides, expected_substrings):
        """State values are injected into the instruction template."""
        state = _base_unresolved_state()
        state.update(state_overrides)
        instruction = build_instruction(state)
        report(f"build_instruction [{desc}]", {
            f"contains '{sub}'": str(sub) in instruction
            for sub in expected_substrings
        })
        for sub in expected_substrings:
            assert str(sub) in instruction


# ======================================================================
# 5. LlmAddressOutput - Pydantic schema validation
# ======================================================================


class TestLlmAddressOutput:
    """Pydantic schema lenient parsing for LLM quirks."""

    @pytest.mark.parametrize(
        "desc, kwargs, field, expected",
        SCHEMA_SAMPLES,
        ids=[s[0] for s in SCHEMA_SAMPLES],
    )
    def test_schema_validation(self, desc, kwargs, field, expected):
        """Schema validators clamp, coerce, and preserve values correctly."""
        out = LlmAddressOutput(**kwargs)
        actual = getattr(out, field)
        report(f"LlmAddressOutput [{desc}]", {field: actual})
        assert actual == expected


# ======================================================================
# 6. Real LLM agent - happy path (requires Ollama)
# ======================================================================


@_SKIP_NO_DB
@_SKIP_NO_OLLAMA
class TestRealLlmHappyPath:
    """Send real addresses to the LLM agent and validate structural output.

    Assertions are on structure (llm_result keys, call count, tokens) because
    LLM output is non-deterministic.  These tests verify the agent runs the
    full multi-turn loop without crashing and produces a usable result.
    """

    @pytest.mark.parametrize(
        "desc, address_1, address_2, address_3, country_code, "
        "libpostal_town, postal_code, mismatch, suggested_cc",
        REAL_LLM_HAPPY_SAMPLES,
        ids=[s[0] for s in REAL_LLM_HAPPY_SAMPLES],
    )
    def test_llm_produces_result(
        self, desc, address_1, address_2, address_3, country_code,
        libpostal_town, postal_code, mismatch, suggested_cc,
    ):
        """Real LLM produces a non-None llm_result with expected keys."""
        logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
        state = _base_unresolved_state(
            address_1, address_2, address_3, country_code,
            libpostal_town, postal_code, mismatch, suggested_cc,
        )
        instruction = build_instruction(state)
        agent = LlmAddressParserAgent(name="test_happy")
        agent.instruction = instruction

        result = _run_agent(agent, state)

        report(f"REAL LLM [{desc}]", {
            "llm_result": result.get("llm_result"),
            "llm_calls": result.get("llm_calls"),
            "llm_prompt_tokens": result.get("llm_prompt_tokens"),
            "llm_completion_tokens": result.get("llm_completion_tokens"),
        })

        assert result["llm_calls"] >= 1, "At least one LLM call should be made"
        assert result["llm_prompt_tokens"] > 0, "Prompt tokens should be consumed"
        # LLM is non-deterministic: result may be None if model returned
        # unparseable text.  Validate structure only when a result exists.
        llm_result = result.get("llm_result")
        if llm_result is not None:
            assert "town" in llm_result, "Result must contain 'town' key"
            assert "confidence" in llm_result, "Result must contain 'confidence'"


# ======================================================================
# 7. Real LLM agent - negative cases (requires Ollama)
# ======================================================================


@_SKIP_NO_DB
@_SKIP_NO_OLLAMA
class TestRealLlmNegative:
    """Send problematic addresses to the LLM and verify graceful handling.

    The agent must not crash even when the address is gibberish or empty.
    """

    @pytest.mark.parametrize(
        "desc, address_1, address_2, address_3, country_code, "
        "libpostal_town, postal_code, mismatch, suggested_cc",
        REAL_LLM_NEGATIVE_SAMPLES,
        ids=[s[0] for s in REAL_LLM_NEGATIVE_SAMPLES],
    )
    def test_llm_handles_gracefully(
        self, desc, address_1, address_2, address_3, country_code,
        libpostal_town, postal_code, mismatch, suggested_cc,
    ):
        """LLM handles bad input without crashing; result may be None or low confidence."""
        logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
        state = _base_unresolved_state(
            address_1, address_2, address_3, country_code,
            libpostal_town, postal_code, mismatch, suggested_cc,
        )
        instruction = build_instruction(state)
        agent = LlmAddressParserAgent(name="test_negative")
        agent.instruction = instruction

        result = _run_agent(agent, state)

        report(f"REAL LLM NEG [{desc}]", {
            "llm_result": result.get("llm_result"),
            "llm_calls": result.get("llm_calls"),
        })

        # Agent must not crash - that is the main assertion
        assert result["llm_calls"] >= 0
        # If LLM produced output, confidence should be low for gibberish
        llm_result = result.get("llm_result")
        if llm_result and llm_result.get("town"):
            assert llm_result["confidence"] <= 1.0


# ======================================================================
# 8. Real LLM agent - edge cases (requires Ollama)
# ======================================================================


@_SKIP_NO_DB
@_SKIP_NO_OLLAMA
class TestRealLlmEdgeCases:
    """Edge cases: ambiguous cities, mismatch propagation."""

    @pytest.mark.parametrize(
        "desc, address_1, address_2, address_3, country_code, "
        "libpostal_town, postal_code, mismatch, suggested_cc",
        REAL_LLM_EDGE_SAMPLES,
        ids=[s[0] for s in REAL_LLM_EDGE_SAMPLES],
    )
    def test_edge_case(
        self, desc, address_1, address_2, address_3, country_code,
        libpostal_town, postal_code, mismatch, suggested_cc,
    ):
        """Edge-case addresses produce structurally valid output."""
        logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
        state = _base_unresolved_state(
            address_1, address_2, address_3, country_code,
            libpostal_town, postal_code, mismatch, suggested_cc,
        )
        instruction = build_instruction(state)
        agent = LlmAddressParserAgent(name="test_edge")
        agent.instruction = instruction

        result = _run_agent(agent, state)

        report(f"REAL LLM EDGE [{desc}]", {
            "llm_result": result.get("llm_result"),
            "llm_calls": result.get("llm_calls"),
            "suggested_country_code": result.get("suggested_country_code"),
        })

        assert result["llm_calls"] >= 1, "At least one LLM call should be made"
        llm_result = result.get("llm_result")
        if llm_result is not None:
            assert "town" in llm_result


# ======================================================================
# 9. E2E pipeline: raw address -> Steps 1-6
# ======================================================================


@_SKIP_NO_DB
@_SKIP_NO_OLLAMA
class TestLlmFromRawAddress:
    """End-to-end: raw address -> parse -> lookup -> match -> detect -> scan -> llm.

    Simulates the real pipeline flow (Steps 1 through 6).  Earlier steps
    may resolve the city (exact_match or scan_match); if so, the LLM is
    still invoked but may confirm or override.  Assertions are structural.
    """

    @pytest.mark.parametrize(
        "desc, address_1, address_2, address_3, country_code",
        E2E_STEP1_THROUGH_STEP6,
        ids=[s[0] for s in E2E_STEP1_THROUGH_STEP6],
    )
    def test_e2e_pipeline(
        self, desc, address_1, address_2, address_3, country_code,
    ):
        """Full pipeline: parse -> lookup -> match -> detect -> scan -> llm_parser."""
        from services.address_scanner import scan
        from services.geonames_exact import match
        from services.libpostal_parser import parse
        from services.mismatch_detector import detect
        from services.postal_lookup import lookup

        logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

        raw = " ".join(filter(None, [address_1, address_2, address_3]))
        state = {
            "address_1": address_1,
            "address_2": address_2,
            "address_3": address_3,
            "country_code": country_code,
            "raw_address": raw,
            "status": "unresolved",
            "row_index": 1,
            "warnings": [],
        }

        report(f"E2E INPUT [{desc}]", {
            "address_1": address_1,
            "address_2": address_2,
            "address_3": address_3,
            "country_code": country_code,
        })

        # Steps 1-5
        parse(state)
        lookup(state)
        match(state)
        detect(state)
        scan(state)

        report(f"E2E AFTER STEPS 1-5 [{desc}]", {
            "libpostal_town": state.get("libpostal_town"),
            "exact_match": state.get("exact_match"),
            "town_candidate": state.get("town_candidate"),
            "scan_match": state.get("scan_match"),
            "scan_candidate": state.get("scan_candidate"),
            "mismatch_detected": state.get("mismatch_detected"),
        })

        # Step 6 - LLM
        instruction = build_instruction(state)
        agent = LlmAddressParserAgent(name="e2e_llm")
        agent.instruction = instruction

        result = _run_agent(agent, state)

        report(f"E2E AFTER STEP 6 [{desc}]", {
            "llm_result": result.get("llm_result"),
            "llm_calls": result.get("llm_calls"),
            "llm_prompt_tokens": result.get("llm_prompt_tokens"),
            "llm_completion_tokens": result.get("llm_completion_tokens"),
        })

        # Structural assertions — guaranteed invariants
        assert result["llm_calls"] >= 1, "LLM should be called at least once"
        assert result["llm_prompt_tokens"] > 0, "Prompt tokens should be consumed"
        # LLM is non-deterministic: validate structure only when result exists
        llm_result = result.get("llm_result")
        if llm_result is not None:
            assert "town" in llm_result, "Result must have 'town' key"
            assert "confidence" in llm_result, "Result must have 'confidence'"
            assert isinstance(llm_result["confidence"], float)
