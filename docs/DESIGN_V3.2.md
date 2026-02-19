# Structured Address AI v3 — ADK Agentic Pipeline Architecture

> **Version:** 3.2 — _18 February 2026_
> **Status:** Proposed — Refactors v2 pipeline as a Google ADK agent workflow
> **Prerequisite:** [DESIGN_V2.md](./DESIGN_V2.md) — production infrastructure, data architecture, security, cost model
> **Audience:** Engineering, Architecture Review

---

## Table of Contents

1. [Purpose & Scope](#1-purpose--scope)
2. [Design Philosophy: Simplicity First](#2-design-philosophy-simplicity-first)
3. [ADK Agent Types — Quick Reference](#3-adk-agent-types--quick-reference)
4. [Architecture Overview](#4-architecture-overview)
5. [Architecture Diagram](#5-architecture-diagram)
6. [Agent Definitions](#6-agent-definitions)
7. [Orchestrator: AddressPipelineAgent](#7-orchestrator-addresspipelineagent)
8. [Session State Contract](#8-session-state-contract)
9. [ADK Runtime Modes](#9-adk-runtime-modes)
10. [Checkpointing & Crash Recovery](#10-checkpointing--crash-recovery)
11. [Observability](#11-observability)
12. [Evaluation Framework](#12-evaluation-framework)
13. [Deployment Matrix](#13-deployment-matrix)
14. [Migration from v2 Design](#14-migration-from-v2-design)
15. [Benefits Summary](#15-benefits-summary)
16. [Risks & Mitigations](#16-risks--mitigations)
17. [Decision Record](#17-decision-record)
18. [Appendix A: ADK Documentation References](#appendix-a-adk-documentation-references)
19. [Appendix B: Proposed File Structure](#appendix-b-proposed-file-structure)

---

## 1. Purpose & Scope

### What This Document Covers

DESIGN_V2.md defines the full production system: infrastructure (GCP Dataflow, Cloud SQL, Redis, GCS), data architecture (GeoNames ETL, PostgreSQL schema), security, compliance, cost model, and the 8-step processing pipeline.

**This document (v3.2) redesigns the pipeline orchestration layer** — how the 8 steps are wired together and executed — using Google ADK (Agent Development Kit). Everything else in v2 remains unchanged.

### What Changed

| Aspect | v2 Design | v3 Design |
|--------|-----------|-----------|
| Pipeline orchestration | Custom Python `pipeline.py` with procedural waterfall | **ADK CustomAgent** orchestrator with 4 sub-agents |
| Step 6 (LLM) | ADK `LlmAgent` with tools — designed in v2 §9.6 | Same — now a first-class sub-agent within the workflow |
| Steps 0–5 | Separate Python functions called sequentially | **1 CustomAgent** (`DeterministicResolverAgent`) with conditional early-exit |
| Steps 7–8 | Inline Python calls | **2 CustomAgents** — `RevalidationAgent` + `PersistAgent` |
| Runtime | Custom FastAPI + Dataflow integration | **ADK Runtime**: `adk web` (dev), `adk api_server` (API), `adk run` (CLI) |
| Observability | Custom OpenTelemetry + Prometheus | ADK built-in event tracing + OpenTelemetry (additive) |
| Checkpointing | Custom `CheckpointedPipeline` wrapping procedural pipeline (v2 §12.6) | **Same strategy** — `CheckpointedBatchProcessor` wraps ADK `Runner` instead (see §10) |
| Dev experience | Run pytest, hit API manually | `adk web` → browser UI with trace inspection, tool call visualization |

### What Stays from v2 (Unchanged)

- GCP infrastructure (Dataflow, Cloud SQL, Memorystore Redis, GCS)
- GeoNames data architecture (PostgreSQL schema, ETL, cities + postal codes + admin1)
- Checkpointing strategy (chunk-based, 1K-row interval, Cloud SQL + GCS) — adapted to wrap ADK Runner (§10)
- Security & compliance controls
- Cost model & scaling targets
- The 8-step pipeline logic itself — only the wiring changes

---

## 2. Design Philosophy: Simplicity First

### The Problem with Over-Agentification

An earlier draft of this document wrapped every pipeline step (0–8) as a separate `CustomAgent` — 10 agent classes total. That was **over-engineered**:

- Steps 0–5 are pure Python functions (normalize, parse, lookup, match, detect, scan). They don't use an LLM, don't need ADK state injection, and don't benefit from being separate agents.
- Wrapping each as a `BaseAgent` subclass adds boilerplate for zero architectural benefit.
- 10 agent classes means 10 files, 10 constructors, 10 Pydantic field declarations — complexity without value.

### The Simplicity Principle

> **Make things agents only when they need to be agents.**

| What needs to be an agent? | Why? |
|----------------------------|------|
| **Orchestrator** (Steps 0→8) | Conditional routing: skip LLM if resolved early. Needs `_run_async_impl`. |
| **LLM parser** (Step 6) | Uses LLM reasoning, tool calling, structured output. Must be `LlmAgent`. |
| **Revalidation** (Step 7) | Safety-net that runs on all paths. Will evolve (multi-source validation, confidence recalculation). Separate agent = independent testing, clear ADK trace event. |
| **Persist** (Step 8) | I/O side effects (Cloud SQL, GCS, review queue). Isolating I/O from logic aids testing and error handling. Separate ADK trace event. |

| What should be plain functions? | Why? |
|----------------------------------|------|
| Steps 0–5 (preprocess, parse, lookup, match, detect, scan) | Pure Python logic. Called sequentially inside the `DeterministicResolverAgent`. Testable with plain pytest — no ADK overhead. |

### Result: 5 Total Agent Classes

```
1 orchestrator + 4 sub-agents = 5 classes

vs. the earlier 1 orchestrator + 9 sub-agents = 10 classes
```

### Why Not SequentialAgent for the Orchestrator?

ADK provides `SequentialAgent` for running sub-agents in strict order. We can't use it because:

1. **Conditional branching** — If `DeterministicResolverAgent` resolves the row (Steps 0–5), we must skip `LlmAddressParserAgent` (Step 6). `SequentialAgent` runs all sub-agents unconditionally.
2. **State-dependent routing** — The orchestrator reads `ctx.session.state["status"]` after Step 5 to decide the next step. `SequentialAgent` has no such hook.

Therefore, the orchestrator is a **`CustomAgent`** (extends `BaseAgent`) with `_run_async_impl` that implements the conditional flow.

### Why Not Tools Instead of Sub-Agents?

In ADK, **tools are LLM-callable** — they're functions that an `LlmAgent` invokes via tool-calling (the LLM decides when/how to call them). A workflow agent or custom agent doesn't "call tools" — it orchestrates **sub-agents**.

If we made Steps 0–5 into "tools" on a single `LlmAgent`, the LLM would decide which tools to call and in what order. That's the pure-agentic approach we rejected in v2 (270× cost, non-deterministic execution).

---

## 3. ADK Agent Types — Quick Reference

From the [ADK Agents documentation](https://google.github.io/adk-docs/agents/):

```
┌─────────────────────────────────────────────────────────────────┐
│                        BaseAgent                                 │
│                    (abstract base class)                         │
│                                                                  │
│  ┌───────────────┐  ┌───────────────┐  ┌─────────────────────┐  │
│  │   LlmAgent    │  │WorkflowAgents │  │   CustomAgent       │  │
│  │               │  │               │  │   (BaseAgent)       │  │
│  │  • LLM-powered│  │• SequentialA. │  │                     │  │
│  │  • Non-determ.│  │• ParallelA.   │  │  • Your own logic   │  │
│  │  • Tool calling│ │• LoopAgent    │  │  • Conditional flow  │  │
│  │  • Reasoning  │  │               │  │  • State management  │  │
│  │               │  │• Deterministic│  │  • Can be determ.    │  │
│  │               │  │• No LLM      │  │    or dynamic        │  │
│  └───────────────┘  └───────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

| Type | Engine | Determinism | Our Use |
|------|--------|-------------|---------|
| `LlmAgent` | LLM (Gemini, Ollama via LiteLLM) | Non-deterministic | `LlmAddressParserAgent` (Step 6) |
| `SequentialAgent` | Predefined order | Deterministic | Not used — our flow has conditionals |
| `ParallelAgent` | Run sub-agents concurrently | Deterministic | Not used — pipeline is sequential |
| `LoopAgent` | Repeat until condition | Deterministic | Not used currently |
| `CustomAgent` | `_run_async_impl` code | Your choice | Orchestrator + 3 deterministic sub-agents |

---

## 4. Architecture Overview

### Agent Hierarchy

```
AddressPipelineAgent (CustomAgent — orchestrator)
  │
  ├── 1. DeterministicResolverAgent (CustomAgent)
  │       Steps 0–5: preprocess → libpostal → postal lookup →
  │       exact match → mismatch detect → fuzzy scan
  │       Contains conditional early-exit logic
  │
  ├── 2. LlmAddressParserAgent (LlmAgent)          ← only LLM agent
  │       Step 6: agentic reasoning with 5 GeoNames tools
  │       SKIPPED if DeterministicResolverAgent resolved the row
  │
  ├── 3. RevalidationAgent (CustomAgent)
  │       Step 7: safety-net GeoNames re-check on resolved town
  │       Always runs regardless of path
  │
  └── 4. PersistAgent (CustomAgent)
          Step 8: write to Cloud SQL + GCS + review queue
          Always runs regardless of path
```

### Agent Responsibilities

| # | Agent | Type | Steps | LLM? | Role |
|---|-------|------|-------|------|------|
| — | `AddressPipelineAgent` | `CustomAgent` | All | ❌ | Orchestrator: accepts input, routes through sub-agents, handles conditional skip |
| 1 | `DeterministicResolverAgent` | `CustomAgent` | 0–5 | ❌ | Runs all deterministic resolution logic as plain function calls. Sets `status=validated` if resolved. |
| 2 | `LlmAddressParserAgent` | `LlmAgent` | 6 | ✅ | Agentic LLM with 5 GeoNames tools. Only called for unresolved rows (~15%). |
| 3 | `RevalidationAgent` | `CustomAgent` | 7 | ❌ | Safety-net: re-validates resolved town against GeoNames. Adjusts confidence. |
| 4 | `PersistAgent` | `CustomAgent` | 8 | ❌ | I/O: writes results to Cloud SQL, GCS, enqueues `needs_review` rows. |

### Flow Summary

```
Input → Orchestrator
         │
         ├─→ DeterministicResolverAgent (Steps 0–5)
         │     │
         │     ├── if resolved ──→ skip LLM ──┐
         │     │                               │
         │     └── if unresolved               │
         │           │                         │
         │           ▼                         │
         │     LlmAddressParserAgent (Step 6)  │
         │           │                         │
         │           ▼                         │
         ├───────────┴─────────────────────────┘
         │
         ├─→ RevalidationAgent (Step 7) — always runs
         │
         └─→ PersistAgent (Step 8) — always runs
```

---

## 5. Architecture Diagram

### Single-Row Flow Through the Agent Pipeline

```
User Input (address row via adk web / adk api_server / adk run)
  │
  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  AddressPipelineAgent (CustomAgent — orchestrator)                    │
│                                                                       │
│  session.state = {                                                    │
│    "address_1": "Via Roma 15",                                        │
│    "address_2": "08042 Barisardo (OG)",                               │
│    "country_code": "IE",                                              │
│    "status": "pending"                                                │
│  }                                                                    │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  DeterministicResolverAgent (CustomAgent — Steps 0–5)           │  │
│  │                                                                  │  │
│  │  Step 0: preprocess(state)                                       │  │
│  │    → normalized_address, extracted_postal_code                    │  │
│  │                                                                  │  │
│  │  Step 1: libpostal_parse(state)                                  │  │
│  │    → town_candidate, street, building, postal_code               │  │
│  │                                                                  │  │
│  │  Step 2: postal_code_lookup(state)                               │  │
│  │    → postal_region, postal_city_hint                             │  │
│  │                                                                  │  │
│  │  Step 3: exact_match(state)                                      │  │
│  │    → if confident match: status="validated" → DONE               │  │
│  │    → if no match: continue                                       │  │
│  │                                                                  │  │
│  │  Step 4: mismatch_detect(state)     ← only if not yet resolved   │  │
│  │    → mismatch_detected, suggested_country_code                   │  │
│  │                                                                  │  │
│  │  Step 5: geonames_scan(state)       ← only if not yet resolved   │  │
│  │    → if confident match: status="validated" → DONE               │  │
│  │    → if no match: status stays "unresolved"                      │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│         │                                                             │
│         ├── if status == "validated" ──→ SKIP Step 6 ──────────┐     │
│         │                                                      │     │
│         ▼  (status == "unresolved")                            │     │
│  ┌─────────────────────────────────────────────────────────┐   │     │
│  │  LlmAddressParserAgent (LlmAgent — Step 6)              │   │     │
│  │  ✨ ONLY AGENT THAT USES AN LLM                         │   │     │
│  │                                                          │   │     │
│  │  Tools: query_city, query_postal_code, query_admin1,     │   │     │
│  │         search_city_fuzzy, list_countries_for_city        │   │     │
│  │                                                          │   │     │
│  │  → state: resolved_town, parser_source="llm_agent"       │   │     │
│  └──────────────────────────────────┬──────────────────────┘   │     │
│                                     │                          │     │
│                                     ▼                          │     │
│  ┌──────────────────────────────────┴──────────────────────┐   │     │
│  │  RevalidationAgent (CustomAgent — Step 7)  ◄────────────┘   │     │
│  │  Always runs. Safety-net GeoNames re-check.              │         │
│  │  → state: final status, confidence_score                 │         │
│  └──────────────────────────────────┬──────────────────────┘         │
│                                     ▼                                │
│  ┌─────────────────────────────────────────────────────────┐         │
│  │  PersistAgent (CustomAgent — Step 8)                     │         │
│  │  Always runs. Cloud SQL + GCS + review queue.            │         │
│  │  → state: final_result                                   │         │
│  └─────────────────────────────────────────────────────────┘         │
│                                                                       │
│  Output: session.state["final_result"]                                │
└──────────────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

1. **Early exit within DeterministicResolverAgent** — If Step 3 (exact match) resolves the row, Steps 4–5 are skipped inside the agent. This is internal conditional logic, not inter-agent routing.
2. **Conditional LLM skip** — The orchestrator checks `state["status"]` after `DeterministicResolverAgent`. If `"validated"`, it skips `LlmAddressParserAgent` entirely. This saves LLM cost on ~85% of rows.
3. **Steps 7–8 always run** — `RevalidationAgent` and `PersistAgent` execute on every path — whether resolved at Step 3, Step 5, or Step 6.
4. **All state in `ctx.session.state`** — No custom dict-passing. ADK manages the session.

---

## 6. Agent Definitions

### 6.1 DeterministicResolverAgent (Steps 0–5)

This agent calls plain Python functions sequentially. The functions read from and write to `ctx.session.state`. The agent contains conditional logic to skip Steps 4–5 if Step 3 resolves the row.

```python
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from typing import AsyncGenerator
from typing_extensions import override

# Business logic — plain Python functions in services/
from services.normalizer import preprocess
from services.libpostal_parser import libpostal_parse
from services.postal_lookup import postal_code_lookup
from services.geonames_exact import exact_match
from services.mismatch_detector import mismatch_detect
from services.address_scanner import geonames_scan


class DeterministicResolverAgent(BaseAgent):
    """Steps 0–5: deterministic address resolution using rule-based logic.

    Calls plain Python functions sequentially. If the address is resolved
    at Step 3 (exact match) or Step 5 (fuzzy scan), sets status='validated'
    and returns early — the orchestrator will then skip the LLM agent.
    """

    model_config = {"arbitrary_types_allowed": True}

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        # ── Step 0: Preprocess ──────────────────────────────────
        # NFKC normalize, casefold, whitespace collapse, extract postal code
        preprocess(state)

        if state.get("validation_errors"):
            state["status"] = "rejected"
            state["review_reason"] = "validation_error"
            yield Event(author=self.name, content=None)
            return

        # ── Step 1: libpostal Parse ─────────────────────────────
        # Extract town_candidate, street, building, postal_code, state
        libpostal_parse(state)

        # ── Step 2: Postal Code Cross-Reference ─────────────────
        # Postal code → region + city hint (disambiguation signal)
        postal_code_lookup(state)

        # ── Step 3: GeoNames Exact Match ────────────────────────
        # Exact match with disambiguation (postal, admin1, population)
        exact_match(state)

        if state.get("status") == "validated":
            # Resolved! Orchestrator will skip LLM agent.
            yield Event(author=self.name, content=None)
            return

        # ── Step 4: Country-Code Mismatch Detection ─────────────
        # Cross-validate country_code against address signals
        mismatch_detect(state)

        # ── Step 5: GeoNames Fuzzy Scan ─────────────────────────
        # Scan raw address text against city lexicon
        geonames_scan(state)

        # status is now either "validated" (resolved at Step 5) or "unresolved"
        yield Event(author=self.name, content=None)
```

**Key point:** The 6 functions (`preprocess`, `libpostal_parse`, etc.) are **plain Python** in `services/`. They mutate `state` (a dict) and have zero ADK dependency. They're tested with plain pytest.

### 6.2 LlmAddressParserAgent (Step 6)

The only agent that uses an LLM. Defined as an `LlmAgent` with 5 GeoNames-backed tools.

```python
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.genai import types
from pydantic import BaseModel, Field


# ── Tool Functions ──────────────────────────────────────────────
# ADK auto-wraps plain Python functions as FunctionTool.
# Type hints + docstrings → ADK generates the schema for the LLM.

def query_city(name: str, country_code: str) -> list[dict]:
    """Look up a city by name within a specific country.
    Returns list of matches with geonameid, official_name, admin1, population.
    Returns empty list if no match found."""
    return db.query_city(name, country_code)


def query_postal_code(postal_code: str, country_code: str) -> list[dict]:
    """Look up a postal code in a specific country.
    Returns list of places with place_name, admin1_name, admin1_code.
    Useful for disambiguation and country-code verification."""
    return db.query_postal_code(postal_code, country_code)


def query_admin1(country_code: str, admin1_code: str) -> dict | None:
    """Get the admin1 region name for a country + admin1 code.
    Example: query_admin1('US', 'IL') → {'name': 'Illinois', 'code': 'US.IL'}"""
    return db.query_admin1(country_code, admin1_code)


def search_city_fuzzy(partial_name: str, country_code: str, limit: int = 5) -> list[dict]:
    """Fuzzy search for a city name within a country using trigram similarity.
    Returns top matches sorted by similarity score.
    Useful when the address contains misspellings or abbreviations."""
    return db.search_city_fuzzy(partial_name, country_code, limit)


def list_countries_for_city(name: str) -> list[dict]:
    """Find ALL countries where a city name exists in GeoNames.
    Returns list of {country_code, official_name, population} sorted by population.
    Useful for detecting country-code mismatches."""
    return db.list_countries_for_city(name)


# ── Structured Output Schema ───────────────────────────────────

class LlmAddressOutput(BaseModel):
    """Structured output schema enforced on the LLM response."""
    town: str | None = Field(description="Verified city/town name from GeoNames")
    street: str | None = Field(description="Street address")
    building: str | None = Field(description="Building or house number")
    postal_code: str | None = Field(description="Postal/ZIP code")
    status: str = Field(description="'validated' or 'needs_review'")
    suggested_country_code: str | None = Field(description="Corrected country code if mismatch detected")
    reasoning: str = Field(description="Brief explanation of the resolution")


# ── System Prompt ───────────────────────────────────────────────
# {var} syntax → ADK auto-fills from session.state

SYSTEM_PROMPT = """You are an address parsing specialist. Your task is to extract
the correct town/city name from an unstructured address.

Context from previous pipeline steps:
- Town candidate: {town_candidate}
- Country code: {country_code}
- Postal code: {extracted_postal_code}
- Postal region hint: {postal_region}
- Mismatch detected: {mismatch_detected}
- Suggested country code: {suggested_country_code}

You have access to GeoNames tools to verify your answers against real geographic data.
ALWAYS use tools to verify — never guess.

Workflow:
1. Read the address and identify candidate town/city names.
2. Use query_city() to check if candidates exist in the given country.
3. If not found, consider:
   - Is the country_code wrong? Use list_countries_for_city() to check.
   - Is there a postal code? Use query_postal_code() for disambiguation.
   - Is the spelling off? Use search_city_fuzzy() to find close matches.
4. Return your structured output with reasoning.

Rules:
- If you cannot verify a town with tools, set status to "needs_review".
- Never fabricate a town name — only return names confirmed by tools.
- If you detect a country-code mismatch, include suggested_country_code."""


# ── Agent Definition ────────────────────────────────────────────

# Production: Gemini via Vertex AI
llm_address_parser_agent = LlmAgent(
    name="LlmAddressParserAgent",
    model="gemini-2.0-flash",
    description="Parses unstructured addresses using LLM reasoning with GeoNames tool access.",
    instruction=SYSTEM_PROMPT,
    tools=[query_city, query_postal_code, query_admin1,
           search_city_fuzzy, list_countries_for_city],
    output_schema=LlmAddressOutput,
    output_key="llm_result",       # auto-stored in session.state["llm_result"]
    generate_content_config=types.GenerateContentConfig(
        temperature=0.0,            # deterministic output
        max_output_tokens=500,
    ),
)

# Dev/local: Ollama via LiteLLM wrapper
llm_address_parser_agent_local = LlmAgent(
    name="LlmAddressParserAgent",
    model=LiteLlm(model="ollama_chat/qwen2.5-coder:14b"),
    description="Parses unstructured addresses using LLM reasoning with GeoNames tool access.",
    instruction=SYSTEM_PROMPT,
    tools=[query_city, query_postal_code, query_admin1,
           search_city_fuzzy, list_countries_for_city],
    output_schema=LlmAddressOutput,
    output_key="llm_result",
)
```

**ADK features used:**
- `instruction` uses `{var}` template syntax — ADK auto-fills from `session.state`
- `tools` are plain Python functions — ADK auto-wraps them as `FunctionTool`
- `output_schema` enforces structured JSON output via Pydantic
- `output_key="llm_result"` auto-stores the result in `session.state["llm_result"]`

### 6.3 RevalidationAgent (Step 7)

```python
from services.geonames_revalidation import revalidate_against_geonames


class RevalidationAgent(BaseAgent):
    """Step 7: Re-validate resolved town against GeoNames (safety net).

    Runs on EVERY path — whether resolved at Step 3, 5, or 6.
    Confirms the resolved town exists in GeoNames for the effective country code.
    Adjusts confidence score. Downgrades status to 'needs_review' if check fails.
    """

    model_config = {"arbitrary_types_allowed": True}

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        # If LLM agent ran, extract its result into standard state keys
        llm_result = state.get("llm_result")
        if llm_result and state.get("parser_source") != "llm_agent":
            state["resolved_town"] = llm_result.get("town")
            state["parser_source"] = "llm_agent"
            if llm_result.get("suggested_country_code"):
                state["suggested_country_code"] = llm_result["suggested_country_code"]

        resolved_town = state.get("resolved_town")
        effective_cc = state.get("suggested_country_code") or state["country_code"]

        if resolved_town:
            revalidation = revalidate_against_geonames(
                town=resolved_town,
                country_code=effective_cc,
            )
            if revalidation["confirmed"]:
                state["status"] = "validated"
                state["confidence_score"] = max(
                    state.get("confidence_score", 0),
                    revalidation["confidence"],
                )
            else:
                state["status"] = "needs_review"
                state["review_reason"] = "revalidation_failed"
        else:
            state["status"] = (
                "needs_review" if state.get("town_candidate") else "rejected"
            )
            state["review_reason"] = "no_town_resolved"

        yield Event(author=self.name, content=None)
```

### 6.4 PersistAgent (Step 8)

```python
from services.persistence import (
    build_result_record,
    persist_to_cloud_sql,
    write_to_gcs_output,
    enqueue_for_review,
)


class PersistAgent(BaseAgent):
    """Step 8: Persist results to Cloud SQL, GCS, and review queue.

    Runs on EVERY path. Builds the final result record from session state,
    writes to Cloud SQL and GCS, and enqueues rows with status='needs_review'.
    """

    model_config = {"arbitrary_types_allowed": True}

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        result = build_result_record(state)

        await persist_to_cloud_sql(result)
        await write_to_gcs_output(result)

        if state.get("status") == "needs_review":
            await enqueue_for_review(result)

        state["final_result"] = result
        yield Event(author=self.name, content=None)
```

---

## 7. Orchestrator: AddressPipelineAgent

The orchestrator is a `CustomAgent` that wires the 4 sub-agents together with conditional logic.

```python
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from typing import AsyncGenerator
from typing_extensions import override
import logging

logger = logging.getLogger(__name__)


class AddressPipelineAgent(BaseAgent):
    """Top-level orchestrator for the address parsing pipeline.

    Sequential flow with one conditional branch:
      1. DeterministicResolverAgent (Steps 0–5)
      2. LlmAddressParserAgent (Step 6) — SKIPPED if resolved at Steps 0–5
      3. RevalidationAgent (Step 7)     — always runs
      4. PersistAgent (Step 8)          — always runs
    """

    # ── Sub-agent declarations (Pydantic fields) ──
    deterministic_resolver: DeterministicResolverAgent
    llm_agent: LlmAgent
    revalidation_agent: RevalidationAgent
    persist_agent: PersistAgent

    model_config = {"arbitrary_types_allowed": True}

    def __init__(
        self,
        name: str,
        deterministic_resolver: DeterministicResolverAgent,
        llm_agent: LlmAgent,
        revalidation_agent: RevalidationAgent,
        persist_agent: PersistAgent,
    ):
        sub_agents_list = [
            deterministic_resolver,
            llm_agent,
            revalidation_agent,
            persist_agent,
        ]

        super().__init__(
            name=name,
            deterministic_resolver=deterministic_resolver,
            llm_agent=llm_agent,
            revalidation_agent=revalidation_agent,
            persist_agent=persist_agent,
            sub_agents=sub_agents_list,
        )

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        logger.info(f"[{self.name}] Starting pipeline")

        # ── Sub-agent 1: Deterministic Resolver (Steps 0–5) ────
        logger.info(f"[{self.name}] → DeterministicResolverAgent")
        async for event in self.deterministic_resolver.run_async(ctx):
            yield event

        # Short-circuit: if rejected at validation, skip everything
        if ctx.session.state.get("status") == "rejected":
            logger.warning(f"[{self.name}] Row rejected at validation — skip to persist")
            async for event in self.persist_agent.run_async(ctx):
                yield event
            return

        # ── Conditional: Skip LLM if already resolved ──────────
        if ctx.session.state.get("status") == "validated":
            logger.info(f"[{self.name}] ✅ Resolved deterministically — skipping LLM")
        else:
            # ── Sub-agent 2: LLM Parser (Step 6) ───────────────
            logger.info(f"[{self.name}] → LlmAddressParserAgent (unresolved row)")
            async for event in self.llm_agent.run_async(ctx):
                yield event

        # ── Sub-agent 3: Revalidation (Step 7) — always runs ──
        logger.info(f"[{self.name}] → RevalidationAgent")
        async for event in self.revalidation_agent.run_async(ctx):
            yield event

        # ── Sub-agent 4: Persist (Step 8) — always runs ───────
        logger.info(f"[{self.name}] → PersistAgent")
        async for event in self.persist_agent.run_async(ctx):
            yield event

        logger.info(
            f"[{self.name}] Pipeline complete — "
            f"status: {ctx.session.state.get('status')}, "
            f"source: {ctx.session.state.get('parser_source')}"
        )
```

### Instantiation and Runner

```python
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# ── Create the 4 sub-agents ──
resolver = DeterministicResolverAgent(name="DeterministicResolverAgent")
revalidation = RevalidationAgent(name="RevalidationAgent")
persist = PersistAgent(name="PersistAgent")

# ── Create the orchestrator ──
pipeline_agent = AddressPipelineAgent(
    name="AddressPipelineAgent",
    deterministic_resolver=resolver,
    llm_agent=llm_address_parser_agent,   # from §6.2
    revalidation_agent=revalidation,
    persist_agent=persist,
)

# ── Process a single address row ──
async def process_address(address_row: dict) -> dict:
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="address_ai",
        user_id="batch",
        session_id=f"row_{address_row['row_index']}",
        state=address_row,  # initial state = the address row
    )

    runner = Runner(
        agent=pipeline_agent,
        app_name="address_ai",
        session_service=session_service,
    )

    content = types.Content(
        role="user",
        parts=[types.Part(text=f"Process address row {address_row['row_index']}")],
    )

    async for event in runner.run_async(
        user_id="batch",
        session_id=f"row_{address_row['row_index']}",
        new_message=content,
    ):
        pass  # consume events; results are in session.state

    final_session = await session_service.get_session(
        app_name="address_ai",
        user_id="batch",
        session_id=f"row_{address_row['row_index']}",
    )
    return final_session.state.get("final_result")
```

---

## 8. Session State Contract

All agents share a single `ctx.session.state` dictionary. Here is the complete contract:

### 8.1 Input Keys (Set Before Pipeline Starts)

| Key | Type | Source | Required |
|-----|------|--------|----------|
| `address_1` | `str` | Input CSV/API | ✅ |
| `address_2` | `str` | Input CSV/API | ❌ |
| `address_3` | `str` | Input CSV/API | ❌ |
| `country_code` | `str` (ISO 3166-1 alpha-2) | Input CSV/API | ✅ |
| `row_index` | `int` | Batch processor | ✅ |
| `job_id` | `str` (UUID) | Batch processor | ✅ |

### 8.2 Intermediate Keys (Set by DeterministicResolverAgent)

| Key | Set By Step | Type | Description |
|-----|------------|------|-------------|
| `normalized_address` | 0 | `str` | NFKC-normalized, casefolded, whitespace-collapsed |
| `extracted_postal_code` | 0 | `str \| None` | Postal code extracted via regex |
| `validation_errors` | 0 | `list[str]` | Schema validation errors (empty = valid) |
| `town_candidate` | 1 | `str` | libpostal-extracted town name |
| `street` | 1 | `str` | libpostal-extracted street |
| `building` | 1 | `str` | libpostal-extracted building number |
| `libpostal_postal_code` | 1 | `str` | libpostal-extracted postal code |
| `libpostal_state` | 1 | `str` | libpostal-extracted state/province |
| `postal_region` | 2 | `str \| None` | Admin1 region name from postal code lookup |
| `postal_city_hint` | 2 | `str \| None` | City name hint from postal code |
| `postal_admin1_code` | 2 | `str \| None` | Admin1 code from postal code |
| `exact_match_result` | 3 | `dict \| None` | Full match record if found |
| `mismatch_detected` | 4 | `bool` | Whether country-code mismatch was detected |
| `suggested_country_code` | 4 | `str \| None` | Corrected country code |
| `mismatch_signals` | 4 | `list[str]` | Signals that triggered mismatch |

### 8.3 Keys Set by LlmAddressParserAgent

| Key | Type | Description |
|-----|------|-------------|
| `llm_result` | `dict` | LLM agent's structured output (auto-set via `output_key`) |

### 8.4 Output Keys (Final Result)

| Key | Set By | Type | Description |
|-----|--------|------|-------------|
| `status` | Steps 3/5/7 | `str` | `validated`, `needs_review`, or `rejected` |
| `resolved_town` | Steps 3/5/6 | `str \| None` | Final resolved town name |
| `parser_source` | Steps 3/5/6 | `str` | `libpostal`, `geonames_scan`, or `llm_agent` |
| `confidence_score` | Steps 3/5/7 | `float` | 0.00–1.00 |
| `geonames_id` | Steps 3/5 | `int \| None` | GeoNames ID of matched city |
| `review_reason` | Step 7 | `str \| None` | Why it's `needs_review` |
| `final_result` | Step 8 | `dict` | Complete result record for persistence |

### 8.5 State Template Injection (LLM Agent Only)

ADK's `{var}` syntax in `LlmAgent.instruction` auto-fills from `session.state`:

```
{town_candidate}           → from Step 1
{country_code}             → from input
{extracted_postal_code}    → from Step 0
{postal_region}            → from Step 2
{mismatch_detected}        → from Step 4
{suggested_country_code}   → from Step 4
```

No manual prompt construction needed — ADK handles the injection.

---

## 9. ADK Runtime Modes

ADK provides three built-in runtimes. The **same agent code** runs in all three — zero code changes.

### 9.1 Dev Mode: `adk web`

```bash
adk web --port 8000

# Opens browser at http://localhost:8000
# Features:
#  - Chat interface to send address rows
#  - Real-time event trace (see every sub-agent's events)
#  - Tool call visualization (see LLM's GeoNames queries in Step 6)
#  - Session state inspection (see state after each agent)
#  - Auto-reload on code changes
```

**Use case:** Development, debugging, demos.

### 9.2 API Mode: `adk api_server`

```bash
adk api_server --port 8000

# REST endpoints:
# POST /apps/address_pipeline_agent/users/{user_id}/sessions/{session_id}
#   → Create session with initial state (address row)
#
# POST /run
#   → Process address row, return all events
#   Body: { "appName": "address_pipeline_agent", "userId": "batch",
#           "sessionId": "row_42", "newMessage": {...} }
#
# POST /run_sse
#   → Stream events as SSE (Server-Sent Events)
#
# GET /apps/address_pipeline_agent/users/{user_id}/sessions/{session_id}
#   → Get session with final state (results)
#
# Swagger UI: http://localhost:8000/docs
```

**Use case:** Replaces custom FastAPI API from v2. Cloud Run deployment.

### 9.3 CLI Mode: `adk run`

```bash
adk run address_ai

# Interactive terminal mode. Use case: quick testing, CI pipelines.
```

### 9.4 Batch Processing via Dataflow

For batch processing (millions of rows via GCP Dataflow), we embed the ADK `Runner` inside a Dataflow `ParDo` transform:

```python
import apache_beam as beam
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types


class ProcessAddressFn(beam.DoFn):
    """Dataflow ParDo that runs the ADK pipeline for each row."""

    def setup(self):
        """Called once per worker — initialize the agent."""
        self.pipeline_agent = create_pipeline_agent()
        self.session_service = InMemorySessionService()

    async def process(self, row):
        session = await self.session_service.create_session(
            app_name="address_ai",
            user_id="dataflow",
            session_id=f"row_{row['row_index']}",
            state=row,
        )

        runner = Runner(
            agent=self.pipeline_agent,
            app_name="address_ai",
            session_service=self.session_service,
        )

        content = types.Content(
            role="user",
            parts=[types.Part(text=f"Process row {row['row_index']}")],
        )

        async for event in runner.run_async(
            user_id="dataflow",
            session_id=f"row_{row['row_index']}",
            new_message=content,
        ):
            pass

        final = await self.session_service.get_session(
            app_name="address_ai",
            user_id="dataflow",
            session_id=f"row_{row['row_index']}",
        )
        yield final.state.get("final_result")


# Dataflow pipeline
with beam.Pipeline(options=pipeline_options) as p:
    (
        p
        | "ReadCSV" >> beam.io.ReadFromText("gs://address-input/batch.csv")
        | "ParseCSV" >> beam.Map(parse_csv_row)
        | "ProcessAddress" >> beam.ParDo(ProcessAddressFn())
        | "WriteResults" >> beam.io.WriteToText("gs://address-output/results")
    )
```

**Same agent code** runs in `adk web` (dev), `adk api_server` (API), and Dataflow (batch).

### 9.5 Local Batch Mode: `batch_runner.py`

For local development and testing, `batch_runner.py` reads an Excel/CSV file from `data/inputs/`, loops over each row, runs the ADK pipeline, and writes results back. 

#### 9.5.1 File I/O Services

Both live in `services/` — plain Python, no ADK dependency. Carried forward from the POC `io_excel.py` with format expansion.

**`services/io_reader.py`** — reads input files:

```python
"""Read address input files (Excel or CSV) into row dicts."""

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Column alias mapping — normalizes common variants to canonical names
_ALIASES = {
    "addr_1": "address_1", "addr_2": "address_2", "addr_3": "address_3",
    "addr1": "address_1",  "addr2": "address_2",  "addr3": "address_3",
    "address1": "address_1", "address2": "address_2", "address3": "address_3",
    "line_1": "address_1", "line_2": "address_2", "line_3": "address_3",
    "address_line_1": "address_1", "address_line_2": "address_2",
    "address_line_3": "address_3",
    "cc": "country_code", "country": "country_code",
}


def read_input(filepath: str | Path) -> list[dict]:
    """Read an Excel (.xlsx) or CSV file and return a list of row dicts.

    Each dict contains: address_1, address_2, address_3, country_code,
    plus a synthetic row_index key for session tracking.

    Args:
        filepath: Path to the input file.

    Returns:
        List of dicts, one per valid row. Invalid rows are logged and skipped.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Input file not found: {filepath}")

    # Read based on file extension
    ext = filepath.suffix.lower()
    if ext in (".xlsx", ".xls"):
        # keep_default_na=False prevents pandas converting "NA" (Namibia) to NaN
        df = pd.read_excel(filepath, dtype=str, keep_default_na=False)
    elif ext == ".csv":
        df = pd.read_csv(filepath, dtype=str, keep_default_na=False)
    else:
        raise ValueError(f"Unsupported file format: '{ext}'. Use .xlsx, .xls, or .csv")

    # Normalize column names
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    df.columns = [_ALIASES.get(c, c) for c in df.columns]

    if "country_code" not in df.columns:
        raise ValueError(
            f"Input file must contain a 'country_code' column. "
            f"Found: {list(df.columns)}"
        )

    rows: list[dict] = []
    for idx, row_data in df.iterrows():
        try:
            row = {
                "row_index": idx,
                "address_1": _clean(row_data.get("address_1")),
                "address_2": _clean(row_data.get("address_2")),
                "address_3": _clean(row_data.get("address_3")),
                "country_code": str(row_data.get("country_code", "")).strip(),
            }
            rows.append(row)
        except Exception as e:
            logger.warning("Skipping row %d: %s", idx, e)

    logger.info("Loaded %d valid rows from %d total", len(rows), len(df))
    return rows


def _clean(value: Optional[object]) -> Optional[str]:
    """Coerce a cell value to a clean string or None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s if s else None
```

**`services/io_writer.py`** — writes output files:

```python
"""Write pipeline results to Excel or CSV."""

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_OUTPUT_COLUMNS = [
    # Original input (audit trail)
    "address_1", "address_2", "address_3", "country_code",
    # Extracted fields
    "town", "street", "building", "postal_code",
    # Pipeline metadata
    "status", "confidence_score", "parser_source",
    "geonames_match", "geonames_id", "normalized_town",
    "warnings", "review_reason",
]


def write_output(
    results: list[dict],
    filepath: str | Path,
    sheet_name: str = "Results",
) -> Path:
    """Write pipeline result dicts to an Excel or CSV file.

    Format is determined by file extension (.xlsx or .csv).

    Args:
        results:    List of result dicts (from session.state["final_result"]).
        filepath:   Output file path.
        sheet_name: Worksheet name (Excel only).

    Returns:
        Resolved Path of the written file.
    """
    filepath = Path(filepath).resolve()
    filepath.parent.mkdir(parents=True, exist_ok=True)

    # Flatten warnings list to semicolon-separated string
    for r in results:
        if isinstance(r.get("warnings"), list):
            r["warnings"] = "; ".join(r["warnings"])

    df = pd.DataFrame(results)

    # Reorder to canonical column order
    ordered = [c for c in _OUTPUT_COLUMNS if c in df.columns]
    extra = [c for c in df.columns if c not in _OUTPUT_COLUMNS]
    df = df[ordered + extra]

    ext = filepath.suffix.lower()
    if ext == ".csv":
        df.to_csv(filepath, index=False)
    else:
        df.to_excel(filepath, sheet_name=sheet_name, index=False)

    logger.info("Output written to %s (%d rows)", filepath, len(results))
    return filepath
```

#### 9.5.2 Batch Runner

`batch_runner.py` sits at the **repository root** and is the CLI entry point for local batch processing. It reads a file, loops over rows, runs the ADK agent pipeline per row, and writes the results.

```python
"""Local batch runner — read file → ADK agent pipeline → write results.

Usage:
    python batch_runner.py --input data/samples/test_addresses.xlsx
    python batch_runner.py --input data/samples/batch.csv --output data/output/results.xlsx
"""

import argparse
import asyncio
import logging
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from address_pipeline_agent.agent import root_agent
from services.io_reader import read_input
from services.io_writer import write_output

logger = logging.getLogger(__name__)


async def process_batch(input_path: str, output_path: str) -> None:
    """Read input file, run pipeline per row, write results."""
    rows = read_input(input_path)
    logger.info("Processing %d rows from %s", len(rows), input_path)

    session_service = InMemorySessionService()
    runner = Runner(
        agent=root_agent,
        app_name="address_pipeline_agent",
        session_service=session_service,
    )

    results: list[dict] = []
    for row in rows:
        session_id = f"row_{row['row_index']}"

        await session_service.create_session(
            app_name="address_pipeline_agent",
            user_id="batch",
            session_id=session_id,
            state=row,
        )

        content = types.Content(
            role="user",
            parts=[types.Part(text=f"Process address row {row['row_index']}")],
        )

        async for event in runner.run_async(
            user_id="batch",
            session_id=session_id,
            new_message=content,
        ):
            pass  # consume events; results are in session.state

        final = await session_service.get_session(
            app_name="address_pipeline_agent",
            user_id="batch",
            session_id=session_id,
        )
        result = final.state.get("final_result", {})
        results.append(result)

    write_output(results, output_path)
    logger.info("Batch complete: %d rows → %s", len(results), output_path)


def main():
    parser = argparse.ArgumentParser(description="Local batch address processing")
    parser.add_argument("--input", required=True, help="Input file (Excel or CSV)")
    parser.add_argument(
        "--output",
        default=None,
        help="Output file path (default: data/output/<input_stem>_results.xlsx)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = (
        Path(args.output)
        if args.output
        else Path("data/output") / f"{input_path.stem}_results.xlsx"
    )

    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    asyncio.run(process_batch(str(input_path), str(output_path)))


if __name__ == "__main__":
    main()
```

#### 9.5.3 Usage

```bash
# Process an Excel file (output defaults to data/output/test_addresses_results.xlsx)
python batch_runner.py --input data/samples/test_addresses.xlsx

# Process a CSV with explicit output path
python batch_runner.py --input data/samples/batch.csv --output data/output/results.csv
```

#### 9.5.4 Data Flow Summary

```
data/samples/test_addresses.xlsx
  │
  ▼
services/io_reader.py  →  read_input()  →  list[dict]   ← one dict per row
  │
  ▼  (loop per row)
batch_runner.py  →  Runner.run_async()  →  session.state  ← agent pipeline
  │
  ▼
services/io_writer.py  →  write_output()  →  data/output/test_addresses_results.xlsx
```

| Mode | File Reader | File Writer | Entry Point |
|------|-------------|-------------|-------------|
| **Local batch** | `services/io_reader.py` | `services/io_writer.py` | `batch_runner.py` |
| **`adk web`** | N/A (manual chat) | N/A (on-screen) | `adk web` |
| **`adk api_server`** | N/A (caller POSTs JSON) | N/A (JSON response) | `adk api_server` |
| **Dataflow** | `beam.io.ReadFromText` | `beam.io.WriteToText` | `dataflow/pipeline.py` |

---

## 10. Checkpointing & Crash Recovery

> Carries forward v2 §12.6. A 5M-row batch takes ~1–2 hours. Without checkpointing, a crash at row 4M loses all progress. This section adapts the v2 chunk-based checkpointing strategy to the ADK Runner-in-Dataflow pattern.

### 10.1 Strategy: Chunk-Based Write-Ahead Partial Results

```
┌────────────────────────────────────────────────────────────────┐
│  Checkpointing Flow (per Dataflow job)                         │
│                                                                │
│  CSV Input (5M rows)                                           │
│    │                                                           │
│    ├─ Chunk 1 (rows 1–1,000)                                   │
│    │    ├─ ProcessAddressFn runs ADK Runner per row             │
│    │    ├─ Write results to Cloud SQL (batch INSERT)           │
│    │    ├─ Write partial CSV to GCS:                           │
│    │    │    gs://output/job_abc/chunk_00001.csv                │
│    │    ├─ Update job progress: processed_rows = 1,000         │
│    │    └─ ✅ Checkpoint committed                              │
│    │                                                           │
│    ├─ Chunk 2 (rows 1,001–2,000)                               │
│    │    ├─ ProcessAddressFn → Write → Update progress          │
│    │    └─ ✅ Checkpoint committed                              │
│    │                                                           │
│    ├─ ...                                                      │
│    │                                                           │
│    ├─ 💥 CRASH at row 4,000,042                                │
│    │                                                           │
│    │  Recovery:                                                │
│    │    1. Query Cloud SQL: last committed chunk = 4000         │
│    │    2. Resume from row 4,000,001                            │
│    │    3. Re-process only remaining chunks via ADK Runner     │
│    │    4. Merge partial CSVs into final output                 │
│    │                                                           │
│    └─ Chunk 5000 (rows 4,999,001–5,000,000)                    │
│         └─ ✅ Job complete                                      │
│                                                                │
│  Final: merge gs://output/job_abc/chunk_*.csv                  │
│       → gs://output/job_abc/results_final.csv                   │
└────────────────────────────────────────────────────────────────┘
```

### 10.2 Checkpoint Granularity

| Parameter | Default | Notes |
|-----------|---------|-------|
| `CHECKPOINT_INTERVAL_ROWS` | 1,000 | Rows per checkpoint. At ~2KB/row, each commit is ~2MB — negligible overhead for Cloud SQL batch INSERT. |
| `CHECKPOINT_INTERVAL_SECONDS` | 60 (1 min) | Time-based fallback if row throughput is slow (e.g., many LLM calls in a chunk). |
| `CHECKPOINT_TARGET` | `cloud_sql` | Where progress is recorded. Cloud SQL (primary) + GCS (partial CSVs as backup). |

### 10.3 Implementation: ADK Runner with Checkpointed Chunks

Dataflow's **built-in checkpointing** handles worker-level fault tolerance — if a worker dies, Dataflow reassigns its bundle to another worker. However, this is within a single job run. If the entire job fails (OOM, quota exceeded, network partition), Dataflow does NOT auto-resume.

**Application-level checkpointing** covers the job-level restart case. We wrap the `ProcessAddressFn` from §9.4 inside a chunk-based checkpoint manager:

```python
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types


class CheckpointedBatchProcessor:
    """Wraps ADK Runner with chunk-based checkpointing.

    Each chunk of CHECKPOINT_INTERVAL_ROWS is processed through the
    ADK pipeline, results are committed atomically to Cloud SQL,
    and a partial CSV is written to GCS as a recovery point.
    """

    def __init__(self, job_id: str, chunk_size: int = 1_000):
        self.job_id = job_id
        self.chunk_size = chunk_size
        self.pipeline_agent = create_pipeline_agent()
        self.session_service = InMemorySessionService()

    def get_resume_offset(self) -> int:
        """Query Cloud SQL for last committed chunk."""
        row = db.execute(
            "SELECT processed_rows FROM jobs WHERE job_id = %s",
            (self.job_id,)
        ).fetchone()
        return row["processed_rows"] if row else 0

    async def process_row_via_adk(self, row: dict) -> dict:
        """Run a single row through the ADK agent pipeline."""
        session = await self.session_service.create_session(
            app_name="address_ai",
            user_id="dataflow",
            session_id=f"row_{row['row_index']}",
            state=row,
        )

        runner = Runner(
            agent=self.pipeline_agent,
            app_name="address_ai",
            session_service=self.session_service,
        )

        content = types.Content(
            role="user",
            parts=[types.Part(text=f"Process row {row['row_index']}")],
        )

        async for event in runner.run_async(
            user_id="dataflow",
            session_id=f"row_{row['row_index']}",
            new_message=content,
        ):
            pass

        final = await self.session_service.get_session(
            app_name="address_ai",
            user_id="dataflow",
            session_id=f"row_{row['row_index']}",
        )
        return final.state.get("final_result")

    async def process_chunk(self, chunk: list[dict], chunk_num: int):
        """Process one chunk through ADK pipeline and commit checkpoint."""
        results = [await self.process_row_via_adk(row) for row in chunk]

        # Atomic: write results + update progress in one transaction
        with db.transaction():
            db.bulk_insert("address_results", results)
            db.execute(
                "UPDATE jobs SET processed_rows = %s WHERE job_id = %s",
                (chunk_num * self.chunk_size, self.job_id)
            )

        # Write partial CSV to GCS (outside transaction — idempotent)
        gcs.upload(
            f"gs://output/{self.job_id}/chunk_{chunk_num:04d}.csv",
            to_csv(results)
        )
```

### 10.4 Idempotency

Checkpointing requires idempotent writes — re-processing a chunk must produce the same result:

| Component | Idempotency Mechanism |
|-----------|----------------------|
| Cloud SQL results | `UNIQUE (job_id, row_index)` constraint + `ON CONFLICT DO UPDATE` (upsert) |
| GCS partial CSVs | Overwrite same chunk filename (GCS write is atomic per object) |
| Job progress | `UPDATE` is naturally idempotent |
| LLM calls | Deterministic (temperature=0) + response cache in Redis |
| ADK session state | `InMemorySessionService` — fresh per row, no stale state |

### 10.5 Crash Scenarios & Recovery

| Scenario | Data Loss | Recovery |
|----------|-----------|----------|
| Single Dataflow worker dies | 0 rows | Dataflow auto-retries the failed bundle on another worker |
| Entire Dataflow job fails | ≤ 1,000 rows (1 chunk) | Restart job with `--resume` flag; skips committed chunks |
| Cloud SQL connection lost mid-chunk | ≤ 1,000 rows | Transaction rolls back; chunk retried on next attempt |
| Redis cache lost | 0 rows | Cache miss → DB fallback. LLM responses re-fetched (slower, no data loss) |
| GCS partial CSV write fails | 0 rows | Results already in Cloud SQL. CSV re-generated from DB at job completion |
| ADK Runner error (agent exception) | 1 row | Row marked `status=error` in result; chunk continues. Logged for investigation. |

### 10.6 Monitoring Checkpoints

| Metric | Purpose |
|--------|---------|
| `address_checkpoint_committed_total` | Track checkpointing frequency |
| `address_checkpoint_duration_seconds` | Detect slow commits (DB bottleneck) |
| `address_job_resume_total` | How often jobs are resumed (crash frequency indicator) |
| `address_chunk_reprocess_total` | Rows re-processed after resume (waste indicator) |

### 10.7 Relationship to ADK Sessions

The checkpointing layer is **outside ADK** — it wraps the ADK `Runner` invocation. ADK's `InMemorySessionService` is per-row and ephemeral (created, used, discarded). The checkpointing layer tracks progress at the **chunk level** in Cloud SQL, independent of ADK session lifecycle.

```
Checkpointing layer (Cloud SQL)      ADK layer (InMemorySessionService)
┌───────────────────────┐           ┌───────────────────────────────┐
│  Job: abc              │           │  Session: row_42              │
│  processed_rows: 3000  │           │  state: {address_1, town...}  │
│  chunk_003 committed   │  wraps →  │  Lifecycle: create → run → GC │
│                        │           │  No persistence needed        │
└───────────────────────┘           └───────────────────────────────┘
```

This separation means:
- ADK sessions are **stateless per row** — no session storage cost at scale
- Checkpoint progress is **durable in Cloud SQL** — survives job restarts
- The two concerns don't interfere with each other

---

## 11. Observability

### 11.1 Built-in Trace (adk web)

Every sub-agent emits ADK `Event` objects. The `adk web` UI displays them as a trace:

```
Trace: AddressPipelineAgent (row_42)
  │
  ├── DeterministicResolverAgent (15ms)
  │     └─ state: town_candidate="barisardo", status="unresolved",
  │        mismatch_detected=true, suggested_cc="IT"
  │
  ├── LlmAddressParserAgent (2400ms)     ← LLM sub-agent
  │     ├─ tool_call: query_city("barisardo", "IE") → []
  │     ├─ tool_call: list_countries_for_city("barisardo") → [{IT}]
  │     ├─ tool_call: query_postal_code("08042", "IT") → [{Bari Sardo}]
  │     └─ state: llm_result={town: "Barisardo", status: "validated"}
  │
  ├── RevalidationAgent (3ms)
  │     └─ state: status="validated", confidence=0.75
  │
  └── PersistAgent (10ms)
        └─ state: final_result={...}

  Total: ~2428ms
```

For a row resolved deterministically (no LLM):

```
Trace: AddressPipelineAgent (row_7)
  │
  ├── DeterministicResolverAgent (12ms)
  │     └─ state: status="validated", resolved_town="Dublin",
  │        parser_source="libpostal"
  │
  ├── [LlmAddressParserAgent — SKIPPED]
  │
  ├── RevalidationAgent (2ms)
  │     └─ state: status="validated", confidence=0.95
  │
  └── PersistAgent (8ms)
        └─ state: final_result={...}

  Total: ~22ms
```

**4 trace events per row** (or 3 if LLM skipped) — clean, readable, actionable.

### 11.2 Observability Comparison

| Capability | v2 (Custom) | v3 (ADK, 4 sub-agents) |
|-----------|-------------|------------------------|
| Per-agent latency | Custom OpenTelemetry spans | ✅ Auto-generated from agent events |
| LLM tool call inspection | Custom logging | ✅ Built into `adk web` UI |
| Session state after each agent | Not available | ✅ State viewer in `adk web` |
| LLM prompt/response viewing | Custom debug logging | ✅ Full prompt trace in UI |
| Internal Steps 0–5 detail | Separate spans per step | Python logging inside DeterministicResolverAgent |
| Production tracing | OpenTelemetry (keep) | OpenTelemetry + ADK callbacks |

> **Note:** Steps 0–5 appear as a single trace event. For per-step visibility, use Python `logging` inside `DeterministicResolverAgent`. For production, add OpenTelemetry spans inside each service function for fine-grained tracing.

### 11.3 OpenTelemetry Integration

ADK supports [callbacks](https://google.github.io/adk-docs/callbacks/) for third-party observability (Comet Opik, Jaeger, Datadog). The v2 Prometheus metrics and Grafana dashboards remain unchanged — they're infrastructure-level. ADK adds agent-level visibility on top.

---

## 12. Evaluation Framework

### 12.1 ADK Evaluation

```python
# eval_dataset.json — benchmark addresses with expected results
[
    {
        "input": "Via Roma 15, 08042 Barisardo (OG)",
        "context": {"country_code": "IE"},
        "expected_output": {
            "town": "Barisardo",
            "status": "validated",
            "suggested_country_code": "IT"
        }
    },
    {
        "input": "123 Main St, Springfield",
        "context": {"country_code": "US"},
        "expected_output": {
            "town": "Springfield",
            "status": "validated"
        }
    }
]
```

### 12.2 Evaluation Criteria

| Criterion | Description | Target |
|-----------|-------------|--------|
| **Town accuracy** | Does `resolved_town` match expected? | ≥ 95% |
| **Status correctness** | Does final `status` match expected? | ≥ 98% |
| **Mismatch detection** | Are wrong country codes flagged? | ≥ 90% |
| **False positive rate** | Wrong town marked `validated` | 0% (hard gate) |
| **LLM skip rate** | % of rows resolved without LLM | ≥ 80% |
| **Deterministic path p95** | End-to-end latency without LLM | < 500ms |
| **LLM path p95** | End-to-end latency with LLM | < 5s |

### 12.3 Running Evaluations

```bash
# ADK evaluation framework
adk eval --agent address_ai --dataset tests/benchmark/eval_dataset.json

# Existing pytest suite (v2 tests still work)
pytest tests/test_pipeline_e2e.py -v

# Services-level tests (no ADK, plain pytest)
pytest tests/test_services/ -v
```

---

## 13. Deployment Matrix

### 13.1 Environment → Runtime Mapping

| Environment | ADK Runtime | LLM Provider | Notes |
|-------------|-------------|-------------|-------|
| **Local dev** | `adk web` | Ollama (via LiteLLM) | Trace inspection in browser |
| **Local batch** | `adk run` or pytest | Ollama | Quick multi-row testing |
| **CI/test** | pytest + `Runner` | Mock / Ollama | Automated tests |
| **Staging API** | `adk api_server` on Cloud Run | Vertex AI Gemini Flash | Auto-generated REST endpoints |
| **Staging batch** | `Runner` in Dataflow | Vertex AI Gemini Flash | Same agent code as API |
| **Production API** | `adk api_server` on Cloud Run | Vertex AI Gemini Flash | Production quota + autoscaling |
| **Production batch** | `Runner` in Dataflow | Vertex AI Gemini Flash | 5M rows/day target |

### 13.2 Cloud Run Deployment

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y libpostal-dev
RUN pip install google-adk litellm

COPY address_pipeline_agent/ /app/address_pipeline_agent/
WORKDIR /app

CMD ["adk", "api_server", "--host", "0.0.0.0", "--port", "8080"]
```

### 13.3 What ADK Replaces

| v2 Component | v3 Status |
|-------------|-----------|
| Custom FastAPI (`api/routes.py`) | **Replaced** by `adk api_server` |
| Custom pipeline orchestrator (`pipeline.py`) | **Replaced** by `AddressPipelineAgent` |
| Custom dev debug tooling | **Replaced** by `adk web` |
| Custom OTel trace setup | **Simplified** — ADK generates agent events; keep OTel for infra |
| Custom evaluation harness | **Augmented** — ADK eval + existing pytest benchmark |

---

## 14. Migration from v2 Design

### What Changes in v2 Documents

| v2 Section | Impact | Action |
|-----------|--------|--------|
| §4 Architecture | Minor | Add note: "Orchestration uses ADK — see DESIGN_V3.md" |
| §6 Pipeline Steps | Unchanged | Step logic identical; only wiring changes |
| §9.6 Agentic Workflow | **Superseded** | V3 is the definitive agentic design |
| §11 API Layer | **Simplified** | `adk api_server` replaces custom FastAPI |
| §12.6 Checkpointing | **Adapted** | Same strategy; wraps ADK `Runner` instead of procedural pipeline (v3 §10) |
| §16 Deployment | Updated | Cloud Run runs `adk api_server` |
| All other sections | Unchanged | Infrastructure, data, security, cost model stay |

### Migration Steps

| Phase | Task | Effort |
|-------|------|--------|
| 1 | Install `google-adk`, set up project structure | 0.5 day |
| 2 | Extract Steps 0–5 logic into `services/` functions | 1–2 days |
| 3 | Implement `DeterministicResolverAgent` (wraps service functions) | 0.5 day |
| 4 | Implement `LlmAddressParserAgent` (from v2 §9.6) | 1 day |
| 5 | Implement `RevalidationAgent` + `PersistAgent` | 0.5 day |
| 6 | Implement `AddressPipelineAgent` orchestrator | 0.5 day |
| 7 | Test with `adk web` — verify trace, state flow, conditional skip | 1 day |
| 8 | Wire into Dataflow (`Runner` in `ParDo`) | 1 day |
| 9 | Replace FastAPI with `adk api_server` on Cloud Run | 0.5 day |
| 10 | Set up ADK evaluation with benchmark dataset | 0.5 day |
| **Total** | | **~7 days** |

---

## 15. Benefits Summary

| # | Benefit | Details |
|---|---------|---------|
| 1 | **Simple architecture** | 1 orchestrator + 4 sub-agents. Steps 0–5 are plain functions. Easy to understand. |
| 2 | **Unified runtime** | `adk web` (dev) → `adk api_server` (API) → `Runner` (batch). Same code, three runtimes. |
| 3 | **Free observability** | 4 clean trace events per row in `adk web`. LLM tool calls fully visible. |
| 4 | **No custom API** | `adk api_server` auto-generates `/run`, `/run_sse`, session management. |
| 5 | **Built-in evaluation** | ADK eval + existing 500+ address benchmark. |
| 6 | **State management** | `ctx.session.state` is the single source of truth. No custom dict-passing. |
| 7 | **LLM abstraction** | `"gemini-2.0-flash"` (prod) or `LiteLlm("ollama_chat/...")` (dev). One line to switch. |
| 8 | **Testability** | Services: plain pytest (no ADK). Agents: ADK Runner tests. Both are fast. |
| 9 | **Cloud deployment** | ADK deploys natively to Cloud Run, GKE, Agent Engine. |
| 10 | **Low coupling** | All business logic in `services/` — zero ADK dependency. Can unwire ADK in ~2 days. |

### What We Give Up

| Concern | Assessment |
|---------|-----------|
| ADK dependency | Young ecosystem (2025). Mitigation: business logic is plain Python — ADK is just wiring. |
| Steps 0–5 trace detail | Single trace event for Steps 0–5. Mitigation: Python logging + optional OTel spans. |
| Custom FastAPI control | Lose fine-grained endpoint customization. Mitigation: `adk api_server` covers 95% of needs. |

---

## 16. Risks & Mitigations

| # | Risk | Prob. | Impact | Mitigation |
|---|------|-------|--------|------------|
| 1 | ADK Runner overhead in Dataflow | Low | Medium | Runner + InMemorySessionService < 1ms/row. Profile in staging. |
| 2 | ADK breaking changes | Medium | Medium | Pin version. Business logic is portable (plain Python). |
| 3 | `adk api_server` missing feature | Low | Low | Add FastAPI middleware alongside. |
| 4 | Ollama tool calling issues | Low | Medium | Test with qwen2.5-coder, gemma3. Fall back to prompt-based. |
| 5 | Session state size | Low | Low | ~2KB/row max. InMemorySessionService handles trivially. |
| 6 | Team unfamiliarity | Medium | Medium | 5 classes total. `adk web` is self-documenting. 1-day onboarding. |

---

## 17. Decision Record

### ADR-001: Pipeline as ADK Agent Workflow with 4 Sub-Agents

**Date:** 18 February 2026
**Status:** Proposed

**Context:**
DESIGN_V2.md designed the pipeline as custom Python with ADK only for Step 6 (LLM agent). ADK's `CustomAgent` pattern allows wrapping the entire pipeline as an agent workflow, gaining unified runtimes, observability, and evaluation.

**Decision:**
Structure the pipeline as 1 `CustomAgent` orchestrator with 4 sub-agents:
1. `DeterministicResolverAgent` (CustomAgent) — Steps 0–5, plain function calls with conditional early-exit
2. `LlmAddressParserAgent` (LlmAgent) — Step 6, conditionally skipped
3. `RevalidationAgent` (CustomAgent) — Step 7, always runs
4. `PersistAgent` (CustomAgent) — Step 8, always runs

Steps 0–5 are **plain Python functions** called within `DeterministicResolverAgent`, not separate agents.

**Rationale:**
- 5 total classes vs. 10 in the earlier over-agentified design
- Steps 0–5 don't benefit from being agents — they're pure functions
- Steps 7 and 8 warrant separate agents: Step 7 (safety-net validation) will evolve independently; Step 8 (I/O) benefits from isolation for testing and error handling
- `CustomAgent` orchestrator (not `SequentialAgent`) because of conditional LLM skip

**Consequences:**
- (+) Simple — easy to understand, implement, and maintain
- (+) Three free runtimes: `adk web`, `adk api_server`, `adk run`
- (+) Clean 4-event trace in `adk web` (vs. 10 events in earlier design)
- (+) Business logic in plain Python — testable without ADK, portable
- (+) ~7 days to implement (vs. ~10 days for 10-agent design)
- (–) Steps 0–5 are a single trace blob — use logging/OTel for detail
- (–) ADK framework dependency (mitigated: can unwire in ~2 days)

**Alternatives Rejected:**
1. **10-agent design** — every step as a `CustomAgent`. Over-engineered. Added boilerplate without benefit for deterministic steps.
2. **2-agent design** — only orchestrator + LLM agent. Loses isolation for Step 7 (validation) and Step 8 (I/O).
3. **SequentialAgent orchestrator** — can't do conditional LLM skip.
4. **Tools instead of sub-agents** — tools are LLM-callable; would make the LLM decide execution order (pure-agentic, 270× cost).

---

## Appendix A: ADK Documentation References

| Topic | URL |
|-------|-----|
| Agents overview | https://google.github.io/adk-docs/agents/ |
| LLM Agents | https://google.github.io/adk-docs/agents/llm-agents/ |
| Workflow Agents | https://google.github.io/adk-docs/agents/workflow-agents/ |
| Custom Agents | https://google.github.io/adk-docs/agents/custom-agents/ |
| Function Tools | https://google.github.io/adk-docs/tools-custom/function-tools/ |
| Ollama integration | https://google.github.io/adk-docs/agents/models/ollama/ |
| LiteLLM integration | https://google.github.io/adk-docs/agents/models/litellm/ |
| Runtime (web/CLI/API) | https://google.github.io/adk-docs/runtime/ |
| Web Interface | https://google.github.io/adk-docs/runtime/web-interface/ |
| API Server | https://google.github.io/adk-docs/runtime/api-server/ |
| Evaluation | https://google.github.io/adk-docs/evaluate/ |
| Sessions & State | https://google.github.io/adk-docs/sessions/state/ |
| Callbacks | https://google.github.io/adk-docs/callbacks/ |
| Cloud Run deployment | https://google.github.io/adk-docs/deploy/cloud-run/ |
| Agent Engine | https://google.github.io/adk-docs/deploy/agent-engine/ |

---

## Appendix B: Proposed File Structure

The project follows **ADK conventions**: each agent is a Python package (folder with `__init__.py` + `agent.py`). The top-level `address_pipeline_agent/` directory is the ADK app — `adk web` and `adk run` discover it by folder name from the parent directory. There is **no `src/` directory**; the agent package sits at the repository root.

```
structured_address_ai/                 # Repository root
│
├── address_pipeline_agent/            # ← ADK app — adk web/run discovers this
│   ├── __init__.py                    # from . import agent
│   ├── agent.py                       # root_agent = AddressPipelineAgent(...)
│   │
│   ├── sub_agents/                    # 4 sub-agent packages
│   │   ├── __init__.py
│   │   │
│   │   ├── deterministic_resolver/    # Steps 0–5: rule-based resolution
│   │   │   ├── __init__.py            # from . import agent
│   │   │   └── agent.py              # DeterministicResolverAgent (CustomAgent)
│   │   │
│   │   ├── llm_parser/               # Step 6: LLM-based address parsing
│   │   │   ├── __init__.py            # from . import agent
│   │   │   ├── agent.py              # LlmAddressParserAgent (LlmAgent)
│   │   │   └── tools.py              # 5 GeoNames tool functions for LLM
│   │   │
│   │   ├── revalidation/             # Step 7: GeoNames re-validation
│   │   │   ├── __init__.py            # from . import agent
│   │   │   └── agent.py              # RevalidationAgent (CustomAgent)
│   │   │
│   │   └── persist/                   # Step 8: result persistence
│   │       ├── __init__.py            # from . import agent
│   │       └── agent.py              # PersistAgent (CustomAgent)
│
├── utils/                             # Shared utilities — config, schemas, prompts
│   ├── __init__.py
│   ├── config.py                      # Settings (model, DB path, endpoints)
│   ├── schemas.py                     # Pydantic models (LlmAddressOutput, etc.)
│   └── prompts.py                     # LLM system prompt for Step 6
│
├── services/                          # Business logic — PLAIN PYTHON, no ADK
│   ├── __init__.py
│   ├── io_reader.py                   # Read Excel/CSV input → list[dict]
│   ├── io_writer.py                   # Write list[dict] results → Excel/CSV
│   ├── normalizer.py                  # preprocess() — Step 0
│   ├── libpostal_parser.py            # libpostal_parse() — Step 1
│   ├── postal_lookup.py               # postal_code_lookup() — Step 2
│   ├── geonames_exact.py              # exact_match() — Step 3
│   ├── mismatch_detector.py           # mismatch_detect() — Step 4
│   ├── address_scanner.py             # geonames_scan() — Step 5
│   ├── geonames_revalidation.py       # revalidate_against_geonames() — Step 7
│   ├── geonames_repo.py               # GeoNames DB query layer (shared)
│   └── persistence.py                 # Cloud SQL + GCS + review queue — Step 8
│
├── dataflow/
│   ├── pipeline.py                    # Apache Beam pipeline with ProcessAddressFn
│   └── config.py                      # Dataflow job parameters
│
├── tests/
│   ├── test_agents/                   # Agent-level tests (with ADK Runner)
│   │   ├── test_orchestrator.py       # Full pipeline tests
│   │   ├── test_deterministic.py      # DeterministicResolverAgent
│   │   └── test_llm_agent.py         # LlmAddressParserAgent (mocked LLM)
│   ├── test_services/                 # Service tests — plain pytest, no ADK
│   │   ├── test_normalizer.py
│   │   ├── test_geonames_exact.py
│   │   ├── test_mismatch_detector.py
│   │   └── ...
│   └── benchmark/
│       └── eval_dataset.json          # 500+ curated addresses
│
├── data/
│   ├── database/
│   │   └── geonames.db               # SQLite (304 MB) — cities, variants, postal
│   ├── reference/                     # Raw GeoNames files (cities, postal, etc.)
│   ├── input/                        # Test CSV input files
│   └── output/                        # Pipeline output files
│
├── docs/
│   ├── DESIGN.md                      # POC design (v1.2)
│   ├── DESIGN_V2.0.md                # Production infrastructure
│   └── DESIGN_V3.2.md                # This document — ADK pipeline architecture
│
├── src/
│   └── batch_runner.py                # CLI entry point: python -m src.batch_runner
├── requirements.txt
├── .env                               # API keys, model config (git-ignored)
├── .env.example                       # Template with all configurable env vars
├── .gitignore
└── README.md
```

### ADK Entry-Point Convention

Each `__init__.py` contains exactly one line:

```python
from . import agent
```

The top-level `address_pipeline_agent/agent.py` exports `root_agent`:

```python
# address_pipeline_agent/agent.py
from .sub_agents.deterministic_resolver.agent import deterministic_resolver_agent
from .sub_agents.llm_parser.agent import llm_parser_agent
from .sub_agents.revalidation.agent import revalidation_agent
from .sub_agents.persist.agent import persist_agent

root_agent = AddressPipelineAgent(
    name="address_pipeline_orchestrator",
    sub_agents=[
        deterministic_resolver_agent,
        llm_parser_agent,
        revalidation_agent,
        persist_agent,
    ],
)
```

`adk web` / `adk run` / `adk api_server` are run **from the repository root** (the parent of `address_pipeline_agent/`). ADK discovers the agent by folder name and renders it in the web UI dropdown.

### Key Design Principle

> **All business logic lives in `services/` — plain Python, zero ADK dependency.**

- `services/` functions take a `state` dict, mutate it, and return. Testable with plain pytest.
- `utils/` holds shared config, Pydantic schemas, and LLM prompts — used by both agents and services.
- Sub-agent `agent.py` files are thin wrappers: read state → call service function → yield Event.
- If ADK is ever removed, `services/` and `utils/` are untouched — only sub-agent `agent.py` files (4 thin files) need rewriting.
- Developers working on matching/disambiguation/scanning don't need to know ADK.
