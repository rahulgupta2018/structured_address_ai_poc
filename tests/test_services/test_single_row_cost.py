"""
Single-row FULL pipeline cost analysis — Morbi address.

Invokes the address through the ADK Runner (same as batch_runner / adk web)
so ALL 8 steps execute — including the real LLM call via Ollama.

Requires:
  - GeoNames DB built    (python -m src.geonames_etl)
  - Ollama running        (ollama serve)
  - Model pulled          (ollama pull qwen2.5-coder:14b)

Usage:
    pytest tests/test_services/test_single_row_cost.py -v -s
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest

# ── Skip if GeoNames DB not built ────────────────────────────────────────────

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "database" / "geonames.db"
_SKIP_REASON = f"GeoNames DB not found at {_DB_PATH} — run: python -m src.geonames_etl"

pytestmark = pytest.mark.skipif(not _DB_PATH.exists(), reason=_SKIP_REASON)


# ── Imports (after skip guard) ───────────────────────────────────────────────

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from address_pipeline_agent.agent import root_agent
from utils.config import LLM_MODEL

# ── Constants ────────────────────────────────────────────────────────────────

APP_NAME = "cost_test"
USER_ID = "test_user"

ADDR_1 = "GROUND FLOOR, SHOP NO 10, SR NO 694 PLOT NO 3P, PATIDAR CHAMBERS"
ADDR_2 = "LAJ STEVE QUART LLP LAIAI, MORBI, GUJR"
ADDR_3 = ""
COUNTRY_CODE = "IN"

# Enable INFO logging so we can see the pipeline steps
logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")


# ── Run one row through the full ADK pipeline ────────────────────────────────

async def _run_single_row() -> dict:
    """Run one address through the full ADK pipeline and return session state."""
    session_service = InMemorySessionService()
    runner = Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=session_service,
    )

    session_id = "morbi_cost_test"
    initial_state = {
        "address_1": ADDR_1,
        "address_2": ADDR_2,
        "address_3": ADDR_3,
        "country_code": COUNTRY_CODE,
        "row_index": 1,
        "job_id": "cost_test",
        "warnings": [],
    }

    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
        state=initial_state,
    )

    trigger = types.Content(
        role="user",
        parts=[types.Part(text="Process this address.")],
    )

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=trigger,
    ):
        pass  # pipeline runs; state accumulates in session

    session = await session_service.get_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
    )
    return dict(session.state)


def run_single_row() -> dict:
    """Sync wrapper."""
    return asyncio.run(_run_single_row())


# ── Cost breakdown printer ───────────────────────────────────────────────────

def print_cost_breakdown(state: dict, elapsed_seconds: float = 0.0) -> None:
    """Print complete cost analysis using ACTUAL token counts from the run."""
    fr = state.get("final_result", {})

    prompt_tokens = state.get("llm_prompt_tokens", 0)
    completion_tokens = state.get("llm_completion_tokens", 0)
    total_tokens = prompt_tokens + completion_tokens
    llm_calls = state.get("llm_calls", 0)
    is_deterministic = (llm_calls == 0)

    print(f"\n{'=' * 70}")
    print(f"  SINGLE ROW COST BREAKDOWN")
    print(f"{'=' * 70}")
    print(f"  Input:")
    print(f"    address_1:    {ADDR_1}")
    print(f"    address_2:    {ADDR_2}")
    print(f"    address_3:    {ADDR_3 or '(empty)'}")
    print(f"    country_code: {COUNTRY_CODE}")
    print(f"  Model:          {LLM_MODEL}")
    print(f"  ⏱  Execution:   {elapsed_seconds:.2f}s")
    print(f"{'─' * 70}")

    print(f"  Pipeline Result (final_result):")
    for k in [
        "status", "town", "country_code", "street", "postal_code",
        "confidence_score", "parser_source", "geonames_match",
        "geonames_id", "mismatch_detected", "suggested_country_code",
        "warnings", "review_reason",
    ]:
        v = fr.get(k)
        if v is not None:
            print(f"    {k:<24} {v}")

    print(f"{'─' * 70}")
    print(f"  Actual Token Usage:")
    print(f"    LLM calls:          {llm_calls}")
    print(f"    Prompt tokens:      {prompt_tokens:,}")
    print(f"    Completion tokens:  {completion_tokens:,}")
    print(f"    Total tokens:       {total_tokens:,}")

    print(f"{'─' * 70}")
    print(f"  Cost for THIS row:")
    if is_deterministic:
        print(f"    ✅ Resolved deterministically — $0.00 (no LLM tokens)")
    else:
        models = [
            ("Gemini 2.0 Flash",  0.10, 0.40),
            ("Gemini 1.5 Flash",  0.075, 0.30),
            ("GPT-4o mini",       0.15, 0.60),
            ("Claude 3.5 Haiku",  0.80, 4.00),
            ("GPT-4o",            2.50, 10.00),
        ]
        print(f"    {'Model':<22}  {'Prompt':>12}  {'Completion':>12}  {'TOTAL':>12}")
        print(f"    {'─' * 62}")
        for name, p_price, c_price in models:
            prompt_cost = prompt_tokens * p_price / 1_000_000
            compl_cost = completion_tokens * c_price / 1_000_000
            total_cost = prompt_cost + compl_cost
            print(
                f"    {name:<22}  ${prompt_cost:>10.6f}  ${compl_cost:>10.6f}  ${total_cost:>10.6f}"
            )

        cheapest_name, cheapest_pp, cheapest_cp = min(
            models, key=lambda m: prompt_tokens * m[1] / 1e6 + completion_tokens * m[2] / 1e6
        )
        cheapest_total = prompt_tokens * cheapest_pp / 1e6 + completion_tokens * cheapest_cp / 1e6
        print(f"\n    💰 Cheapest: {cheapest_name} @ ${cheapest_total:.6f} / row")

    # Extrapolation
    print(f"\n{'─' * 70}")
    print(f"  Extrapolation (if ALL rows behave like this one):")
    if is_deterministic:
        print(f"    32,000 rows:     $0.00")
        print(f"    30,000,000 rows: $0.00")
        print(f"    🎉 100% deterministic = zero LLM spend!")
    else:
        for vol_label, vol in [("32,000 rows", 32_000), ("30,000,000 rows", 30_000_000)]:
            print(f"\n    {vol_label}:")
            for name, p_price, c_price in [
                ("Gemini 2.0 Flash",  0.10, 0.40),
                ("Gemini 1.5 Flash",  0.075, 0.30),
                ("GPT-4o mini",       0.15, 0.60),
            ]:
                cost = vol * (prompt_tokens * p_price + completion_tokens * c_price) / 1_000_000
                print(f"      {name:<22} ${cost:>12,.2f}")

    # Time extrapolation
    if elapsed_seconds > 0:
        print(f"\n{'─' * 70}")
        print(f"  ⏱  Time Extrapolation ({elapsed_seconds:.2f}s / row, single-threaded):")
        for vol_label, vol in [("32,000 rows", 32_000), ("30,000,000 rows", 30_000_000)]:
            total_sec = vol * elapsed_seconds
            hours = total_sec / 3600
            days = hours / 24
            if days >= 1:
                print(f"    {vol_label}:  {hours:,.0f} hrs ({days:,.1f} days) single-threaded")
            else:
                print(f"    {vol_label}:  {hours:,.1f} hrs single-threaded")
        for conc in [4, 8, 16]:
            sec_30m = 30_000_000 * elapsed_seconds / conc
            days_30m = sec_30m / 86400
            print(f"    30M @ {conc} concurrent:  {days_30m:,.1f} days")

    print(f"{'=' * 70}")


# ── Test class ───────────────────────────────────────────────────────────────

class TestMorbiAddressCost:
    """Full pipeline cost breakdown for the Morbi commercial address."""

    def test_full_pipeline_via_adk_runner(self):
        """Run the Morbi address through the REAL ADK pipeline (all 8 steps).

        This is the same code path as `adk web` and `batch_runner`.
        If Steps 0–5 don't resolve, it calls the REAL LLM via Ollama.

        Requires: Ollama running with the configured model.
        """
        t0 = time.perf_counter()
        state = run_single_row()
        elapsed = time.perf_counter() - t0

        print_cost_breakdown(state, elapsed_seconds=elapsed)

        fr = state.get("final_result")
        assert fr is not None, "Pipeline should produce a final_result"
        assert fr.get("town") is not None, "Pipeline should resolve a town"

        print(f"\n  ✅ Town resolved: {fr['town']}")
        print(f"  ✅ Status: {fr['status']}")
        print(f"  ✅ Parser: {fr['parser_source']}")
        print(f"  ⏱  Time:   {elapsed:.2f}s")
