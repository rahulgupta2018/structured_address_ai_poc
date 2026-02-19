"""
LlmAddressParserAgent — Step 6.

A **BaseAgent** (custom agent) that calls the LLM via LiteLLM to resolve
town names the deterministic pipeline could not handle.

Why BaseAgent and not LlmAgent?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
ADK's ``LlmAgent`` with ``output_schema`` requires the model to call the
synthetic ``set_model_response`` tool.  Local models (Ollama) often return
JSON as **text content** wrapped in markdown fences instead of making a
tool call, causing a Pydantic ``ValidationError``.

The original POC solved this with robust ``_parse_llm_response()`` that
strips fences and extracts JSON.  This agent replicates that approach
within the ADK framework:

1. Multi-turn tool-calling loop (same as LlmAgent would do).
2. When the model returns text instead of a tool call, apply the POC's
   fence-stripping JSON parser.
3. Emit the result via ``state_delta`` so downstream agents can consume it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

import litellm

from services.geonames_repo import (
    list_countries_for_city,
    query_city,
    query_city_by_admin1,
    query_city_fuzzy,
    query_postal_code,
)
from utils.config import (
    LLM_CONCURRENCY,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
)
from utils.schemas import LlmAddressOutput

logger = logging.getLogger(__name__)

# Maximum LLM round-trips (tool calls + final answer)
_MAX_TURNS = 8

# ── LLM concurrency limiter ──────────────────────────────────────────────────
# Limits how many concurrent LLM calls are in-flight to match Ollama's
# OLLAMA_NUM_PARALLEL.  Lazily created (asyncio.Semaphore needs a running
# event loop).

_llm_semaphore: asyncio.Semaphore | None = None


def _get_llm_semaphore() -> asyncio.Semaphore:
    """Return (and lazily create) the module-level LLM semaphore."""
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(LLM_CONCURRENCY)
        logger.info("LLM semaphore created with concurrency=%d", LLM_CONCURRENCY)
    return _llm_semaphore

# ── Tool registry (name → callable) ─────────────────────────────────────────

_TOOL_FUNCTIONS: dict[str, callable] = {
    "query_city": query_city,
    "list_countries_for_city": list_countries_for_city,
    "query_postal_code": query_postal_code,
    "query_city_fuzzy": query_city_fuzzy,
    "query_city_by_admin1": query_city_by_admin1,
}

# Build OpenAI-format tool definitions from docstrings
_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "query_city",
            "description": query_city.__doc__,
            "parameters": {
                "type": "object",
                "properties": {
                    "city_name": {"type": "string"},
                    "country_code": {"type": "string"},
                },
                "required": ["city_name", "country_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_countries_for_city",
            "description": list_countries_for_city.__doc__,
            "parameters": {
                "type": "object",
                "properties": {
                    "city_name": {"type": "string"},
                },
                "required": ["city_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_postal_code",
            "description": query_postal_code.__doc__,
            "parameters": {
                "type": "object",
                "properties": {
                    "postal_code": {"type": "string"},
                    "country_code": {"type": "string"},
                },
                "required": ["postal_code", "country_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_city_fuzzy",
            "description": query_city_fuzzy.__doc__,
            "parameters": {
                "type": "object",
                "properties": {
                    "city_name": {"type": "string"},
                    "country_code": {"type": "string"},
                    "threshold": {"type": "integer", "default": 80},
                },
                "required": ["city_name", "country_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_city_by_admin1",
            "description": query_city_by_admin1.__doc__,
            "parameters": {
                "type": "object",
                "properties": {
                    "city_name": {"type": "string"},
                    "admin1_name": {"type": "string"},
                    "country_code": {"type": "string"},
                },
                "required": ["city_name", "admin1_name", "country_code"],
            },
        },
    },
]


# ── Robust JSON parser (ported from POC's _parse_llm_response) ──────────────

def _parse_llm_text(raw_text: str) -> dict | None:
    """Extract a JSON object from LLM text output.

    Handles:
    - Clean JSON
    - JSON wrapped in ```json ... ``` fences
    - JSON embedded in surrounding prose

    NOTE: Does NOT unwrap tool-call-like {"name":..., "arguments":...}
    wrappers here.  The caller (_detect_text_tool_call / answer check)
    handles that to distinguish tool calls from final answers.
    """
    text = raw_text.strip()
    if not text:
        return None

    # Strip markdown code fences
    if "```" in text:
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Attempt 1: direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # Attempt 2: find first { ... } block
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def _detect_text_tool_call(parsed: dict) -> tuple[str, dict] | None:
    """Check if parsed JSON is a tool call emitted as text.

    Local models (Ollama/qwen) often emit tool calls as text content:
        {"name": "query_postal_code", "arguments": {"postal_code": "1062", ...}}

    Returns (tool_name, arguments_dict) if it looks like a tool call,
    or None if it looks like a final answer.
    """
    if not isinstance(parsed, dict):
        return None
    name = parsed.get("name")
    arguments = parsed.get("arguments")
    if isinstance(name, str) and name in _TOOL_FUNCTIONS and isinstance(arguments, dict):
        return (name, arguments)
    return None


def _execute_tool_call(name: str, arguments: dict) -> str:
    """Execute a tool function and return JSON result string."""
    func = _TOOL_FUNCTIONS.get(name)
    if not func:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        result = func(**arguments)
        return json.dumps({"result": result}, default=str)
    except Exception as exc:
        logger.warning("Tool %s error: %s", name, exc)
        return json.dumps({"error": str(exc)})


def _make_event(author: str, text: str, state_delta: dict | None = None) -> Event:
    """Build an ADK Event with text content and optional state delta."""
    return Event(
        author=author,
        actions=EventActions(state_delta=state_delta or {}),
        content=types.Content(
            role="model",
            parts=[types.Part(text=text)],
        ),
    )


class LlmAddressParserAgent(BaseAgent):
    """Custom agent that calls LLM via LiteLLM with multi-turn tool use.

    Unlike ADK's LlmAgent, this handles Ollama's tendency to return
    JSON as text content instead of proper tool calls.
    """

    # The instruction is dynamically injected by the orchestrator
    instruction: str = ""

    model_config = {"arbitrary_types_allowed": True}

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        snapshot = dict(state)

        # Build messages
        messages = [
            {"role": "system", "content": self.instruction},
            {"role": "user", "content": "Process this address."},
        ]

        llm_result = None

        # Acquire the LLM semaphore for the entire multi-turn loop
        # so that only LLM_CONCURRENCY calls are in-flight at once.
        sem = _get_llm_semaphore()
        async with sem:
          for turn in range(_MAX_TURNS):
            try:
                response = await litellm.acompletion(
                    model=LLM_MODEL,
                    messages=messages,
                    tools=_TOOL_DEFINITIONS,
                    temperature=LLM_TEMPERATURE,
                    max_tokens=LLM_MAX_TOKENS,
                    timeout=LLM_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                logger.warning("LiteLLM call failed (turn %d): %s", turn, exc)
                break

            choice = response.choices[0]
            message = choice.message

            # ── Case 1: Model made tool call(s) ──────────────────────
            if message.tool_calls:
                messages.append(message.model_dump())

                for tool_call in message.tool_calls:
                    fn_name = tool_call.function.name
                    try:
                        fn_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        fn_args = {}

                    # Check for synthetic set_model_response
                    if fn_name == "set_model_response":
                        llm_result = fn_args
                        break

                    logger.debug("Tool call: %s(%s)", fn_name, fn_args)
                    result_str = _execute_tool_call(fn_name, fn_args)
                    logger.debug("Tool result: %s", result_str[:200])

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_str,
                    })

                if llm_result is not None:
                    break
                continue

            # ── Case 2: Model returned text (no tool calls) ──────────
            content_text = message.content or ""

            # Try to parse as JSON (POC approach)
            parsed = _parse_llm_text(content_text)

            if parsed:
                # 2a: Looks like a tool call emitted as text?
                text_tool = _detect_text_tool_call(parsed)
                if text_tool:
                    fn_name, fn_args = text_tool
                    logger.debug("Text-based tool call: %s(%s)", fn_name, fn_args)
                    result_str = _execute_tool_call(fn_name, fn_args)
                    logger.debug("Tool result: %s", result_str[:200])

                    # Feed result back as a user message so model can
                    # incorporate it into its next response
                    messages.append({"role": "assistant", "content": content_text})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Tool '{fn_name}' returned:\n{result_str}\n\n"
                            "Now provide your final answer as a JSON object "
                            "with keys: town, postal_code, confidence, reasoning."
                        ),
                    })
                    continue

                # 2b: Unwrap {"name":"set_model_response","arguments":{...}}
                if parsed.get("name") == "set_model_response" and "arguments" in parsed:
                    parsed = parsed["arguments"]

                # 2c: Looks like a final answer?
                if "town" in parsed or "town_candidate" in parsed:
                    if "town_candidate" in parsed and "town" not in parsed:
                        parsed["town"] = parsed.pop("town_candidate")
                    llm_result = parsed
                    break

            # If unparseable text, nudge model
            messages.append({"role": "assistant", "content": content_text})
            messages.append({
                "role": "user",
                "content": (
                    "Please provide your answer as a JSON object with keys: "
                    "town, postal_code, confidence, reasoning. "
                    "Or use the available tool functions to look up the data."
                ),
            })

            # Empty stop → give up
            if choice.finish_reason == "stop" and not content_text.strip():
                logger.warning("LLM returned empty response on turn %d", turn)
                break

        # ── Build LlmAddressOutput and store in state ────────────────────
        if llm_result:
            try:
                validated = LlmAddressOutput(**llm_result)
                result_dict = validated.model_dump()
            except Exception as exc:
                logger.warning("LlmAddressOutput validation: %s — salvaging", exc)
                result_dict = {
                    "town": llm_result.get("town") or llm_result.get("town_candidate") or "",
                    "postal_code": llm_result.get("postal_code"),
                    "confidence": float(llm_result.get("confidence", 0.0)),
                    "reasoning": str(llm_result.get("reasoning", "")),
                    "suggested_country_code": llm_result.get("suggested_country_code"),
                }

            state["llm_result"] = result_dict
            # Propagate suggested_country_code to session state so
            # revalidation can search the correct country
            scc = result_dict.get("suggested_country_code") or llm_result.get("suggested_country_code")
            if scc:
                state["suggested_country_code"] = scc
            logger.info(
                "LLM resolved: town=%s, confidence=%.2f",
                result_dict.get("town"), result_dict.get("confidence", 0),
            )
        else:
            logger.warning("LLM produced no usable result after %d turns", _MAX_TURNS)
            state["llm_result"] = None

        # Compute state delta
        delta = {}
        for key, value in state.items():
            if key not in snapshot or snapshot[key] != value:
                delta[key] = value

        yield _make_event(
            self.name,
            f"LLM result: {state.get('llm_result')}",
            state_delta=delta,
        )


# ── Module-level agent instance ──────────────────────────────────────────────

llm_parser_agent = LlmAddressParserAgent(
    name="llm_address_parser",
)
