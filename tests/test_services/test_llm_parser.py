"""Tests for LLM parser agent (Step 6) — town resolution via LLM.

Unit tests that mock the LiteLLM call so no real LLM/Ollama is needed.
Validates prompt construction, JSON parsing (clean / fenced / embedded),
tool-call handling (native + text-emitted), multi-turn loops, edge cases,
and state propagation.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from address_pipeline_agent.sub_agents.llm_parser.agent import (
    LlmAddressParserAgent,
    _TOOL_FUNCTIONS,
    _detect_text_tool_call,
    _execute_tool_call,
    _parse_llm_text,
)
from utils.prompts import build_instruction
from utils.schemas import LlmAddressOutput

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with your own data               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Unresolved address — the state that arrives at Step 6
SAMPLE_ADDRESS_1 = "Italy"
SAMPLE_ADDRESS_2 = ""
SAMPLE_ADDRESS_3 = ""
SAMPLE_COUNTRY_CODE = ""
SAMPLE_RAW_ADDRESS = "italy"
SAMPLE_LIBPOSTAL_TOWN = ""
SAMPLE_LIBPOSTAL_POSTAL_CODE = None

# Expected LLM output for the happy-path test
EXPECTED_LLM_TOWN = "Morvi"
EXPECTED_LLM_CONFIDENCE = 0.85
EXPECTED_LLM_REASONING = "Matched via query_city: Morbi is an alternate name for Morvi in Gujarat, India."

# Mismatch scenario
SAMPLE_MISMATCH_TOWN = "Barisardo"
SAMPLE_MISMATCH_STATED_CC = "IE"
SAMPLE_MISMATCH_CORRECT_CC = "IT"

# Fenced-JSON test — LLM wraps answer in markdown code fences
SAMPLE_FENCED_TOWN = "London"
SAMPLE_FENCED_CONFIDENCE = 0.8

# Embedded-JSON test — JSON buried in prose
SAMPLE_EMBEDDED_TOWN = "Dubai"
SAMPLE_EMBEDDED_CONFIDENCE = 0.7

# Nested-fences test
SAMPLE_NESTED_TOWN = "Paris"

# Tool call test — city queried by LLM via query_city tool
SAMPLE_TOOL_CITY = "Morbi"
SAMPLE_TOOL_CC = "IN"
SAMPLE_TOOL_RESULT_NAME = "Morvi"
SAMPLE_TOOL_RESULT_GEONAMEID = 1262775

# Text-emitted tool call test (Ollama quirk)
SAMPLE_TEXT_TOOL_CITY = "Dubai"
SAMPLE_TEXT_TOOL_CC = "AE"
SAMPLE_TEXT_TOOL_GEONAMEID = 292223

# town_candidate key remapping test
SAMPLE_CANDIDATE_TOWN = "Springfield"
SAMPLE_CANDIDATE_CONFIDENCE = 0.7

# set_model_response wrapper test
SAMPLE_WRAPPER_TOWN = "Munich"
SAMPLE_WRAPPER_CONFIDENCE = 0.8

# Token accumulation multi-turn test
SAMPLE_MULTI_TURN_TOWN = "Tokyo"
SAMPLE_MULTI_TURN_CONFIDENCE = 0.9
SAMPLE_MULTI_TURN_PROMPT_1 = 200
SAMPLE_MULTI_TURN_COMPLETION_1 = 30
SAMPLE_MULTI_TURN_PROMPT_2 = 250
SAMPLE_MULTI_TURN_COMPLETION_2 = 40



# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_llm_response(content: str = "", tool_calls=None, usage=None, finish_reason="stop"):
    """Build a mock LiteLLM acompletion response."""
    message = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
    )
    # Add model_dump for tool-call continuation
    if tool_calls:
        message.model_dump = lambda: {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        }
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage_obj = SimpleNamespace(
        prompt_tokens=usage.get("prompt", 100) if usage else 100,
        completion_tokens=usage.get("completion", 50) if usage else 50,
    )
    return SimpleNamespace(choices=[choice], usage=usage_obj)


def _make_tool_call(name: str, arguments: dict, call_id: str = "call_1"):
    """Build a mock tool_call object."""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


def _make_ctx(state: dict):
    """Build a minimal mock InvocationContext with the given state."""
    session = SimpleNamespace(state=state)
    ctx = SimpleNamespace(session=session)
    return ctx


def _run_agent(agent: LlmAddressParserAgent, state: dict) -> dict:
    """Run the agent synchronously and return the final state."""
    ctx = _make_ctx(state)

    async def _run():
        async for _ in agent._run_async_impl(ctx):
            pass
        return dict(ctx.session.state)

    return asyncio.run(_run())


def _base_unresolved_state() -> dict:
    """Return a typical 'unresolved' state dict arriving at Step 6."""
    return {
        "address_1": SAMPLE_ADDRESS_1,
        "address_2": SAMPLE_ADDRESS_2,
        "address_3": SAMPLE_ADDRESS_3,
        "country_code": SAMPLE_COUNTRY_CODE,
        "raw_address": SAMPLE_RAW_ADDRESS,
        "libpostal_town": SAMPLE_LIBPOSTAL_TOWN,
        "libpostal_postal_code": SAMPLE_LIBPOSTAL_POSTAL_CODE,
        "postal_region": None,
        "postal_city_hint": None,
        "mismatch_detected": False,
        "suggested_country_code": None,
        "status": "unresolved",
        "row_index": 1,
        "warnings": [],
    }


# ══════════════════════════════════════════════════════════════════════════════
# 1. _parse_llm_text — JSON extraction from LLM text
# ══════════════════════════════════════════════════════════════════════════════


class TestParseLlmText:
    """Parse JSON from various text formats returned by LLMs."""

    def test_clean_json(self):
        raw = json.dumps({"town": EXPECTED_LLM_TOWN, "confidence": 0.9})
        result = _parse_llm_text(raw)
        report("_parse_llm_text (clean)", {"input": raw, "output": result})
        assert result["town"] == EXPECTED_LLM_TOWN
        assert result["confidence"] == 0.9

    def test_fenced_json(self):
        raw = f'```json\n{{"town": "{SAMPLE_FENCED_TOWN}", "confidence": {SAMPLE_FENCED_CONFIDENCE}}}\n```'
        result = _parse_llm_text(raw)
        report("_parse_llm_text (fenced)", {"input": raw, "output": result})
        assert result["town"] == SAMPLE_FENCED_TOWN

    def test_embedded_json_in_prose(self):
        raw = f'Based on my analysis, the answer is: {{"town": "{SAMPLE_EMBEDDED_TOWN}", "confidence": {SAMPLE_EMBEDDED_CONFIDENCE}}} which I found.'
        result = _parse_llm_text(raw)
        report("_parse_llm_text (embedded)", {"input": raw, "output": result})
        assert result["town"] == SAMPLE_EMBEDDED_TOWN

    def test_empty_string(self):
        result = _parse_llm_text("")
        report("_parse_llm_text (empty)", {"input": "''", "output": result})
        assert result is None

    def test_no_json(self):
        result = _parse_llm_text("I don't know the answer.")
        report("_parse_llm_text (no JSON)", {"input": "I don't know the answer.", "output": result})
        assert result is None

    def test_nested_fences(self):
        raw = f'```\n```json\n{{"town": "{SAMPLE_NESTED_TOWN}"}}\n```\n```'
        result = _parse_llm_text(raw)
        report("_parse_llm_text (nested fences)", {"input": raw, "output": result})
        assert result is not None
        assert result["town"] == SAMPLE_NESTED_TOWN


# ══════════════════════════════════════════════════════════════════════════════
# 2. _detect_text_tool_call — distinguish tool calls from final answers
# ══════════════════════════════════════════════════════════════════════════════


class TestDetectTextToolCall:
    """Detect tool calls emitted as text by local models."""

    def test_valid_tool_call(self):
        parsed = {"name": "query_city", "arguments": {"city_name": SAMPLE_TOOL_CITY, "country_code": SAMPLE_TOOL_CC}}
        result = _detect_text_tool_call(parsed)
        report("_detect_text_tool_call (valid)", {"input": parsed, "output": result})
        assert result is not None
        assert result[0] == "query_city"
        assert result[1]["city_name"] == SAMPLE_TOOL_CITY

    def test_final_answer_not_tool_call(self):
        parsed = {"town": EXPECTED_LLM_TOWN, "confidence": EXPECTED_LLM_CONFIDENCE}
        result = _detect_text_tool_call(parsed)
        report("_detect_text_tool_call (answer)", {"input": parsed, "output": result})
        assert result is None

    def test_unknown_tool_name(self):
        parsed = {"name": "unknown_func", "arguments": {"x": 1}}
        result = _detect_text_tool_call(parsed)
        report("_detect_text_tool_call (unknown)", {"input": parsed, "output": result})
        assert result is None

    def test_missing_arguments_key(self):
        parsed = {"name": "query_city"}
        result = _detect_text_tool_call(parsed)
        report("_detect_text_tool_call (no args key)", {"input": parsed, "output": result})
        assert result is None

    def test_non_dict_input(self):
        result_str = _detect_text_tool_call("not a dict")
        result_none = _detect_text_tool_call(None)
        report("_detect_text_tool_call (non-dict)", {"string": result_str, "None": result_none})
        assert result_str is None
        assert result_none is None


# ══════════════════════════════════════════════════════════════════════════════
# 3. _execute_tool_call — tool dispatch
# ══════════════════════════════════════════════════════════════════════════════


class TestExecuteToolCall:
    """Tool call dispatch and error handling."""

    def test_known_tool(self):
        mock_qc = MagicMock(return_value=[{"geonameid": SAMPLE_TOOL_RESULT_GEONAMEID, "name": SAMPLE_TOOL_RESULT_NAME}])
        with patch.dict(_TOOL_FUNCTIONS, {"query_city": mock_qc}):
            result_str = _execute_tool_call("query_city", {"city_name": SAMPLE_TOOL_CITY, "country_code": SAMPLE_TOOL_CC})
        result = json.loads(result_str)
        report("_execute_tool_call", {"tool": "query_city", "args": {"city_name": SAMPLE_TOOL_CITY}, "result": result})
        assert "result" in result
        assert result["result"][0]["name"] == SAMPLE_TOOL_RESULT_NAME

    def test_unknown_tool(self):
        result_str = _execute_tool_call("nonexistent", {})
        result = json.loads(result_str)
        report("_execute_tool_call (unknown)", {"result": result})
        assert "error" in result

    def test_tool_exception(self):
        mock_qc = MagicMock(side_effect=Exception("DB error"))
        with patch.dict(_TOOL_FUNCTIONS, {"query_city": mock_qc}):
            result_str = _execute_tool_call("query_city", {"city_name": "X", "country_code": "US"})
        result = json.loads(result_str)
        report("_execute_tool_call (exception)", {"result": result})
        assert "error" in result


# ══════════════════════════════════════════════════════════════════════════════
# 4. build_instruction — prompt construction
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildInstruction:
    """Verify the LLM system prompt is correctly constructed from state."""

    def test_all_fields_injected(self):
        state = _base_unresolved_state()
        instruction = build_instruction(state)
        report("build_instruction", {
            "contains address_1": SAMPLE_ADDRESS_1 in instruction,
            "contains country_code": SAMPLE_COUNTRY_CODE in instruction,
            "contains raw_address": SAMPLE_RAW_ADDRESS in instruction,
            "contains libpostal_town": SAMPLE_LIBPOSTAL_TOWN in instruction,
        })
        assert SAMPLE_ADDRESS_1 in instruction
        assert SAMPLE_COUNTRY_CODE in instruction
        assert SAMPLE_RAW_ADDRESS in instruction
        assert SAMPLE_LIBPOSTAL_TOWN in instruction

    def test_missing_fields_default_to_empty(self):
        state = {"country_code": "US"}
        instruction = build_instruction(state)
        report("build_instruction (sparse)", {
            "contains country_code": "US" in instruction,
            "no KeyError": True,
        })
        assert "US" in instruction
        # Should not raise KeyError for missing fields

    def test_mismatch_flag_injected(self):
        state = _base_unresolved_state()
        state["mismatch_detected"] = True
        state["suggested_country_code"] = "IT"
        instruction = build_instruction(state)
        report("build_instruction (mismatch)", {
            "contains mismatch_detected": "True" in instruction,
            "contains IT": "IT" in instruction,
        })
        assert "True" in instruction
        assert "IT" in instruction


# ══════════════════════════════════════════════════════════════════════════════
# 5. LlmAddressParserAgent — full agent tests (mocked LLM)
# ══════════════════════════════════════════════════════════════════════════════


class TestLlmParserAgentCleanJson:
    """LLM returns a clean JSON final answer on the first call."""

    @patch("address_pipeline_agent.sub_agents.llm_parser.agent.litellm")
    def test_happy_path_clean_json(self, mock_litellm):
        """LLM returns correct town as clean JSON text."""
        llm_response = _make_llm_response(
            content=json.dumps({
                "town": EXPECTED_LLM_TOWN,
                "postal_code": None,
                "confidence": EXPECTED_LLM_CONFIDENCE,
                "reasoning": EXPECTED_LLM_REASONING,
            })
        )
        mock_litellm.acompletion = AsyncMock(return_value=llm_response)

        agent = LlmAddressParserAgent(name="test_llm")
        agent.instruction = "Test instruction"
        state = _base_unresolved_state()

        result = _run_agent(agent, state)

        report("LLM happy path", {
            "llm_result": result.get("llm_result"),
            "llm_calls": result.get("llm_calls"),
            "llm_prompt_tokens": result.get("llm_prompt_tokens"),
            "llm_completion_tokens": result.get("llm_completion_tokens"),
        })

        assert result["llm_result"] is not None
        assert result["llm_result"]["town"] == EXPECTED_LLM_TOWN
        assert result["llm_result"]["confidence"] == EXPECTED_LLM_CONFIDENCE
        assert result["llm_calls"] == 1
        assert result["llm_prompt_tokens"] == 100
        assert result["llm_completion_tokens"] == 50

    @patch("address_pipeline_agent.sub_agents.llm_parser.agent.litellm")
    def test_fenced_json_response(self, mock_litellm):
        """LLM wraps JSON in markdown fences (common with Ollama)."""
        llm_response = _make_llm_response(
            content=f'```json\n{{"town": "{SAMPLE_FENCED_TOWN}", "confidence": {SAMPLE_FENCED_CONFIDENCE}, "reasoning": "found"}}\n```'
        )
        mock_litellm.acompletion = AsyncMock(return_value=llm_response)

        agent = LlmAddressParserAgent(name="test_llm")
        agent.instruction = "Test"
        state = _base_unresolved_state()
        result = _run_agent(agent, state)

        report("LLM fenced JSON", {"llm_result": result.get("llm_result")})
        assert result["llm_result"]["town"] == SAMPLE_FENCED_TOWN


class TestLlmParserAgentToolCalls:
    """LLM makes native tool calls before returning a final answer."""

    @patch("address_pipeline_agent.sub_agents.llm_parser.agent.litellm")
    def test_native_tool_call_then_answer(self, mock_litellm):
        """LLM calls query_city, gets result, then returns final JSON."""
        mock_qc = MagicMock(return_value=[{"geonameid": SAMPLE_TOOL_RESULT_GEONAMEID, "name": SAMPLE_TOOL_RESULT_NAME, "country_code": SAMPLE_TOOL_CC}])

        # Turn 1: tool call
        tool_call_response = _make_llm_response(
            tool_calls=[_make_tool_call("query_city", {"city_name": SAMPLE_TOOL_CITY, "country_code": SAMPLE_TOOL_CC})],
        )
        # Turn 2: final answer
        answer_response = _make_llm_response(
            content=json.dumps({
                "town": SAMPLE_TOOL_RESULT_NAME,
                "confidence": EXPECTED_LLM_CONFIDENCE,
                "reasoning": f"Matched {SAMPLE_TOOL_CITY} → {SAMPLE_TOOL_RESULT_NAME}",
            })
        )
        mock_litellm.acompletion = AsyncMock(side_effect=[tool_call_response, answer_response])

        agent = LlmAddressParserAgent(name="test_llm")
        agent.instruction = "Test"
        state = _base_unresolved_state()

        with patch.dict(_TOOL_FUNCTIONS, {"query_city": mock_qc}):
            result = _run_agent(agent, state)

        report("LLM tool call → answer", {
            "llm_result": result.get("llm_result"),
            "llm_calls": result.get("llm_calls"),
        })
        assert result["llm_result"]["town"] == SAMPLE_TOOL_RESULT_NAME
        assert result["llm_calls"] == 2
        mock_qc.assert_called_once_with(city_name=SAMPLE_TOOL_CITY, country_code=SAMPLE_TOOL_CC)

    @patch("address_pipeline_agent.sub_agents.llm_parser.agent.litellm")
    def test_text_emitted_tool_call(self, mock_litellm):
        """LLM emits tool call as text JSON (Ollama quirk)."""
        mock_qc = MagicMock(return_value=[{"geonameid": SAMPLE_TEXT_TOOL_GEONAMEID, "name": SAMPLE_TEXT_TOOL_CITY}])

        # Turn 1: text-based tool call
        text_tool_response = _make_llm_response(
            content=json.dumps({"name": "query_city", "arguments": {"city_name": SAMPLE_TEXT_TOOL_CITY, "country_code": SAMPLE_TEXT_TOOL_CC}})
        )
        # Turn 2: final answer
        answer_response = _make_llm_response(
            content=json.dumps({"town": SAMPLE_TEXT_TOOL_CITY, "confidence": 0.95, "reasoning": "Exact match"})
        )
        mock_litellm.acompletion = AsyncMock(side_effect=[text_tool_response, answer_response])

        agent = LlmAddressParserAgent(name="test_llm")
        agent.instruction = "Test"
        state = _base_unresolved_state()

        with patch.dict(_TOOL_FUNCTIONS, {"query_city": mock_qc}):
            result = _run_agent(agent, state)

        report("LLM text tool call → answer", {
            "llm_result": result.get("llm_result"),
            "llm_calls": result.get("llm_calls"),
        })
        assert result["llm_result"]["town"] == SAMPLE_TEXT_TOOL_CITY
        assert result["llm_calls"] == 2


class TestLlmParserAgentEdgeCases:
    """Edge cases: empty response, exceptions, malformed output."""

    @patch("address_pipeline_agent.sub_agents.llm_parser.agent.litellm")
    def test_empty_response(self, mock_litellm):
        """LLM returns empty content → llm_result should be None."""
        llm_response = _make_llm_response(content="", finish_reason="stop")
        mock_litellm.acompletion = AsyncMock(return_value=llm_response)

        agent = LlmAddressParserAgent(name="test_llm")
        agent.instruction = "Test"
        state = _base_unresolved_state()
        result = _run_agent(agent, state)

        report("LLM empty response", {
            "llm_result": result.get("llm_result"),
            "llm_calls": result.get("llm_calls"),
        })
        assert result["llm_result"] is None
        assert result["llm_calls"] == 1

    @patch("address_pipeline_agent.sub_agents.llm_parser.agent.litellm")
    def test_llm_exception(self, mock_litellm):
        """LiteLLM raises an exception → agent handles gracefully."""
        mock_litellm.acompletion = AsyncMock(side_effect=Exception("Connection refused"))

        agent = LlmAddressParserAgent(name="test_llm")
        agent.instruction = "Test"
        state = _base_unresolved_state()
        result = _run_agent(agent, state)

        report("LLM exception", {
            "llm_result": result.get("llm_result"),
            "llm_calls": result.get("llm_calls"),
        })
        assert result["llm_result"] is None
        assert result["llm_calls"] == 0

    @patch("address_pipeline_agent.sub_agents.llm_parser.agent.litellm")
    def test_town_candidate_key_mapped_to_town(self, mock_litellm):
        """LLM returns 'town_candidate' instead of 'town' → remapped."""
        llm_response = _make_llm_response(
            content=json.dumps({"town_candidate": SAMPLE_CANDIDATE_TOWN, "confidence": SAMPLE_CANDIDATE_CONFIDENCE})
        )
        mock_litellm.acompletion = AsyncMock(return_value=llm_response)

        agent = LlmAddressParserAgent(name="test_llm")
        agent.instruction = "Test"
        state = _base_unresolved_state()
        result = _run_agent(agent, state)

        report("LLM town_candidate → town", {"llm_result": result.get("llm_result")})
        assert result["llm_result"]["town"] == SAMPLE_CANDIDATE_TOWN

    @patch("address_pipeline_agent.sub_agents.llm_parser.agent.litellm")
    def test_set_model_response_wrapper(self, mock_litellm):
        """LLM wraps answer in set_model_response format."""
        llm_response = _make_llm_response(
            content=json.dumps({
                "name": "set_model_response",
                "arguments": {"town": SAMPLE_WRAPPER_TOWN, "confidence": SAMPLE_WRAPPER_CONFIDENCE, "reasoning": "found"},
            })
        )
        mock_litellm.acompletion = AsyncMock(return_value=llm_response)

        agent = LlmAddressParserAgent(name="test_llm")
        agent.instruction = "Test"
        state = _base_unresolved_state()
        result = _run_agent(agent, state)

        report("LLM set_model_response wrapper", {"llm_result": result.get("llm_result")})
        assert result["llm_result"]["town"] == SAMPLE_WRAPPER_TOWN

    @patch("address_pipeline_agent.sub_agents.llm_parser.agent.litellm")
    def test_suggested_country_code_propagated(self, mock_litellm):
        """LLM detects country mismatch → suggested_country_code in state."""
        llm_response = _make_llm_response(
            content=json.dumps({
                "town": SAMPLE_MISMATCH_TOWN,
                "confidence": 0.75,
                "reasoning": "Town is in Italy, not Ireland",
                "suggested_country_code": SAMPLE_MISMATCH_CORRECT_CC,
            })
        )
        mock_litellm.acompletion = AsyncMock(return_value=llm_response)

        agent = LlmAddressParserAgent(name="test_llm")
        agent.instruction = "Test"
        state = _base_unresolved_state()
        state["country_code"] = SAMPLE_MISMATCH_STATED_CC
        result = _run_agent(agent, state)

        report("LLM country mismatch", {
            "llm_result": result.get("llm_result"),
            "suggested_country_code": result.get("suggested_country_code"),
        })
        assert result["llm_result"]["suggested_country_code"] == SAMPLE_MISMATCH_CORRECT_CC
        assert result["suggested_country_code"] == SAMPLE_MISMATCH_CORRECT_CC

    @patch("address_pipeline_agent.sub_agents.llm_parser.agent.litellm")
    def test_token_usage_accumulated(self, mock_litellm):
        """Token counts are accumulated across multi-turn calls."""
        resp1 = _make_llm_response(
            content="Let me check...",
            usage={"prompt": SAMPLE_MULTI_TURN_PROMPT_1, "completion": SAMPLE_MULTI_TURN_COMPLETION_1},
        )
        resp2 = _make_llm_response(
            content=json.dumps({"town": SAMPLE_MULTI_TURN_TOWN, "confidence": SAMPLE_MULTI_TURN_CONFIDENCE}),
            usage={"prompt": SAMPLE_MULTI_TURN_PROMPT_2, "completion": SAMPLE_MULTI_TURN_COMPLETION_2},
        )
        mock_litellm.acompletion = AsyncMock(side_effect=[resp1, resp2])

        agent = LlmAddressParserAgent(name="test_llm")
        agent.instruction = "Test"
        state = _base_unresolved_state()
        result = _run_agent(agent, state)

        expected_prompt = SAMPLE_MULTI_TURN_PROMPT_1 + SAMPLE_MULTI_TURN_PROMPT_2
        expected_completion = SAMPLE_MULTI_TURN_COMPLETION_1 + SAMPLE_MULTI_TURN_COMPLETION_2
        report("LLM token accumulation", {
            "llm_calls": result["llm_calls"],
            "llm_prompt_tokens": result["llm_prompt_tokens"],
            "llm_completion_tokens": result["llm_completion_tokens"],
        })
        assert result["llm_calls"] == 2
        assert result["llm_prompt_tokens"] == expected_prompt
        assert result["llm_completion_tokens"] == expected_completion


# ══════════════════════════════════════════════════════════════════════════════
# 6. LlmAddressOutput schema validation
# ══════════════════════════════════════════════════════════════════════════════


class TestLlmAddressOutput:
    """Pydantic schema lenient parsing for LLM quirks."""

    def test_valid_output(self):
        out = LlmAddressOutput(town=EXPECTED_LLM_TOWN, confidence=EXPECTED_LLM_CONFIDENCE, reasoning="matched")
        report("LlmAddressOutput (valid)", out.model_dump())
        assert out.town == EXPECTED_LLM_TOWN
        assert out.confidence == EXPECTED_LLM_CONFIDENCE

    def test_confidence_clamped_above_1(self):
        """Confidence > 1 treated as percentage scale → divided by 100."""
        out = LlmAddressOutput(town="X", confidence=85)
        report("LlmAddressOutput (confidence 85→0.85)", {"confidence": out.confidence})
        assert out.confidence == 0.85

    def test_confidence_clamped_to_zero(self):
        out = LlmAddressOutput(town="X", confidence=-5)
        report("LlmAddressOutput (confidence -5→0.0)", {"confidence": out.confidence})
        assert out.confidence == 0.0

    def test_none_town_coerced(self):
        out = LlmAddressOutput(town=None)
        report("LlmAddressOutput (None→'')", {"town": out.town})
        assert out.town == ""

    def test_suggested_country_code(self):
        out = LlmAddressOutput(town=SAMPLE_MISMATCH_TOWN, suggested_country_code=SAMPLE_MISMATCH_CORRECT_CC)
        report("LlmAddressOutput (suggested_cc)", {"suggested_country_code": out.suggested_country_code})
        assert out.suggested_country_code == SAMPLE_MISMATCH_CORRECT_CC

    def test_non_numeric_confidence(self):
        out = LlmAddressOutput(town="X", confidence="not_a_number")
        report("LlmAddressOutput (non-numeric→0.0)", {"confidence": out.confidence})
        assert out.confidence == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 7. Real LLM call — runs the actual agent with Ollama (no mocks)
# ══════════════════════════════════════════════════════════════════════════════

import logging
from pathlib import Path
import urllib.request

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "database" / "geonames.db"


def _ollama_available() -> bool:
    """Return True if Ollama is reachable on localhost."""
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _DB_PATH.exists(), reason=f"GeoNames DB not found at {_DB_PATH}")
@pytest.mark.skipif(not _ollama_available(), reason="Ollama not running on localhost:11434")
class TestRealLlmCall:
    """Call the REAL LLM (Ollama) with SAMPLE_ADDRESS data — no mocks.

    Uses the same SAMPLE_ADDRESS_1 / SAMPLE_COUNTRY_CODE constants at the
    top of this file.  Change those values and re-run to test any address.

    Requires:
      - Ollama running      (ollama serve)
      - Model pulled        (ollama pull <model>)
      - GeoNames DB built   (python -m src.geonames_etl)
    """

    def test_real_llm_step6(self):
        """Send SAMPLE_ADDRESS to the real LLM and print the full result."""
        logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

        from utils.config import LLM_MODEL

        state = _base_unresolved_state()
        instruction = build_instruction(state)

        report("REAL LLM — input", {
            "address_1": SAMPLE_ADDRESS_1,
            "address_2": SAMPLE_ADDRESS_2,
            "address_3": SAMPLE_ADDRESS_3,
            "country_code": SAMPLE_COUNTRY_CODE or "(empty)",
            "raw_address": SAMPLE_RAW_ADDRESS,
            "libpostal_town": SAMPLE_LIBPOSTAL_TOWN or "(empty)",
            "model": LLM_MODEL,
        })

        agent = LlmAddressParserAgent(name="real_llm_test")
        agent.instruction = instruction

        result = _run_agent(agent, state)

        report("REAL LLM — output", {
            "llm_result": result.get("llm_result"),
            "llm_calls": result.get("llm_calls"),
            "llm_prompt_tokens": result.get("llm_prompt_tokens"),
            "llm_completion_tokens": result.get("llm_completion_tokens"),
            "status": result.get("status"),
            "warnings": result.get("warnings"),
        })

        # No assertions on LLM content — this test is for manual inspection.
        # Just verify the agent ran without crashing.
        assert result["llm_calls"] >= 1, "LLM should have been called at least once"
