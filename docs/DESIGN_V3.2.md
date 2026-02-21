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
18. [Tuning Parameters & LLM Cost Reduction](#18-tuning-parameters--llm-cost-reduction)
19. [Appendix A: ADK Documentation References](#appendix-a-adk-documentation-references)
20. [Appendix B: Proposed File Structure](#appendix-b-proposed-file-structure)

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

For local development and testing, `batch_runner.py` reads an Excel/CSV file from `data/inputs/`, runs a **two-pass hybrid architecture** (§9.5.5) that resolves deterministic rows as pure Python calls and only creates ADK sessions for the ~43% of rows that need LLM, then writes results back. 

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

#### 9.5.2 Batch Runner — Two-Pass Hybrid Architecture

`src/batch_runner.py` is the CLI entry point for local batch processing. It implements a **two-pass hybrid architecture** that avoids ADK session overhead for deterministic rows while preserving full agentic LLM processing for unresolved rows.

**Why two passes?** In the original single-pass design, *every* row — even the ~57% that resolve deterministically in <5 ms — went through the full ADK machinery (session creation, event streaming, state-delta computation, 4 sub-agent invocations). This added ~50–200 ms of pure framework overhead per deterministic row. At 30M rows, that's ~475 hours of waste.

**Pass 1 — Deterministic (pure Python, no ADK):**

Runs Steps 0–5 as direct service-function calls on every row. If resolved, immediately runs Steps 7–8 (revalidation + persist) — also as direct calls. Zero ADK overhead.

```python
def _resolve_deterministic(row: dict, row_index: int, job_id: str = "") -> dict:
    """Run Steps 0-5 + 7 + 8 as direct service calls.
    Returns state dict with final_result if resolved, or status='unresolved' if not.
    """
    state = {
        "address_1": row.get("address_1") or "",
        "address_2": row.get("address_2") or "",
        "address_3": row.get("address_3") or "",
        "country_code": (row.get("country_code") or "").strip().upper(),
        "row_index": row_index,
        "job_id": job_id,
        "warnings": [],
    }

    normalizer.preprocess(state)          # Step 0
    libpostal_parser.parse(state)         # Step 1
    postal_lookup.lookup(state)           # Step 2
    geonames_exact.match(state)           # Step 3

    if state.get("exact_match"):
        state["status"] = "resolved"
        state["parser_source"] = "libpostal"
        geonames_revalidation.revalidate(state)  # Step 7
        persistence.persist(state)                # Step 8
        return state

    mismatch_detector.detect(state)       # Step 4
    address_scanner.scan(state)           # Step 5

    if state.get("scan_match"):
        state["status"] = "resolved"
        state["parser_source"] = "geonames_scan"
        geonames_revalidation.revalidate(state)  # Step 7
        persistence.persist(state)                # Step 8
        return state

    state["status"] = "unresolved"  # → queued for Pass 2
    return state
```

**Pass 2 — LLM only (ADK sessions):**

Only unresolved rows get ADK sessions. The pre-computed state from Pass 1 is injected as session state. The `DeterministicResolverAgent` detects the pre-computed state (via `status=unresolved` + `raw_address` already populated) and skips, so the orchestrator routes directly to LLM → revalidation → persist.

```python
async def _process_llm_row(
    runner: Runner,
    session_service: InMemorySessionService,
    row_index: int,
    state: dict,           # ← pre-computed state from Pass 1
    semaphore: asyncio.Semaphore,
) -> tuple[int, dict]:
    """Run ADK pipeline for a single unresolved row (Step 6 + 7 + 8)."""
    async with semaphore:
        session_id = f"row_{row_index:06d}"
        await session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID,
            session_id=session_id, state=state,    # inject Pass 1 state
        )
        trigger = types.Content(
            role="user", parts=[types.Part(text="Process this address.")],
        )
        async for event in runner.run_async(
            user_id=USER_ID, session_id=session_id, new_message=trigger,
        ):
            pass

        session = await session_service.get_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id,
        )
        return (row_index, session.state.get("final_result", {}))
```

**Orchestration in `run_batch()`:**

```python
async def run_batch(input_path, output_path, concurrency, batch_size, resume):
    rows = read_input(input_path)

    # ── Pass 1: Deterministic ────────────────────────────────────
    llm_queue = []     # (row_index, partial_state)
    for i, row in enumerate(rows):
        state = _resolve_deterministic(row, i + 1, job_id)
        if state["status"] == "unresolved":
            llm_queue.append((i + 1, state))
        else:
            results[i] = state["final_result"]

    # ── Pass 2: LLM via ADK (only unresolved rows) ──────────────
    if llm_queue:
        runner = Runner(agent=root_agent, ...)
        for batch in batches(llm_queue, batch_size):
            tasks = [_process_llm_row(runner, ..., idx, state, sem)
                     for idx, state in batch]
            await asyncio.gather(*tasks)
            # write checkpoint between batches
```


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
data/input/test_addresses.xlsx
  │
  ▼
services/io_reader.py  →  read_input()  →  list[dict]   ← one dict per row
  │
  ▼  PASS 1 (pure Python, no ADK — all rows)
  │  _resolve_deterministic() → Steps 0-5 + 7 + 8
  │    ├─ resolved? → results[]                           ← ~57% done here
  │    └─ unresolved? → llm_queue[]                       ← ~43% need LLM
  │
  ▼  PASS 2 (ADK sessions — only unresolved rows)
  │  _process_llm_row() → Runner.run_async() → Steps 6 + 7 + 8
  │    └─ results[]
  │
  ▼
services/io_writer.py  →  write_output()  →  data/output/test_addresses_output.csv
```

| Mode | File Reader | File Writer | Entry Point |
|------|-------------|-------------|-------------|
| **Local batch** | `services/io_reader.py` | `services/io_writer.py` | `src/batch_runner.py` |
| **`adk web`** | N/A (manual chat) | N/A (on-screen) | `adk web` |
| **`adk api_server`** | N/A (caller POSTs JSON) | N/A (JSON response) | `adk api_server` |
| **Dataflow** | `beam.io.ReadFromText` | `beam.io.WriteToText` | `dataflow/pipeline.py` |

#### 9.5.5 Two-Pass Performance Impact

The two-pass architecture eliminates ADK session overhead for deterministic rows:

| Metric | Single-pass (old) | Two-pass (current) |
|--------|-------------------|--------------------|
| Deterministic row cost | ~50–200 ms (ADK session + 4 sub-agents + events) | **~1–5 ms** (direct function calls) |
| LLM row cost | ~10–15 s (Ollama) | ~10–15 s (unchanged) |
| 32K rows, 57% deterministic | ~10.5 h | **~9.7 h** (saves ~30–60 min) |
| 30M rows, 57% deterministic | ~475 h wasted on deterministic overhead | **Minutes** for deterministic pass |

**Quality guarantee:** Zero compromise. Both passes call the **identical service functions** — `normalizer.preprocess()`, `geonames_exact.match()`, `geonames_revalidation.revalidate()`, `persistence.persist()`, etc. The only difference is whether those functions are called directly (Pass 1) or via ADK sub-agent wrappers (Pass 2).

**DeterministicResolverAgent fast-exit:** When the ADK pipeline runs for an LLM row in Pass 2, the pre-computed state from Pass 1 is injected. The `DeterministicResolverAgent` detects this (`status=unresolved` + `raw_address` populated) and yields a single skip event — no re-computation.

**When to use which mode:**

| Mode | Architecture | Use Case |
|------|-------------|----------|
| `src/batch_runner.py` (CLI) | Two-pass hybrid | Production batch processing (32K–30M rows) |
| `adk web` | Full ADK (single-pass) | Development, debugging, single-address testing |
| `adk api_server` | Full ADK (single-pass) | REST API, real-time single-address queries |
| Dataflow (future) | Two-pass per bundle | GCP production at scale |

---

## 10. Checkpointing & Crash Recovery

> For a 32K-row batch at ~10s per LLM row, a full run takes ~1 hour. Without checkpointing, a crash at row 25K loses all progress. This section covers both the **implemented local checkpointing** (§10.1) and the **planned production checkpointing** for Dataflow (§10.4+).

### 10.1 Local Checkpointing (Implemented)

`src/batch_runner.py` implements **rolling checkpoint + resume** for local batch runs:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Local Checkpointing Flow (Two-Pass Architecture)                   │
│                                                                     │
│  Input: 32K rows, batch_size=500, concurrency=8                     │
│    │                                                                │
│    ├─ PASS 1: Deterministic (pure Python, no ADK)                   │
│    │    ├─ Loop all 32K rows through _resolve_deterministic()        │
│    │    ├─ ~18K resolved instantly → results[]                       │
│    │    ├─ ~14K unresolved → llm_queue[]                             │
│    │    └─ ✅ Pass 1 complete (~30 seconds)                          │
│    │                                                                │
│    ├─ PASS 2: LLM via ADK (only ~14K unresolved rows)               │
│    │    │                                                           │
│    │    ├─ LLM Batch 1 (rows 1–500 of llm_queue)                    │
│    │    │    ├─ Process 500 rows via ADK Runner (concurrent)         │
│    │    │    ├─ Write rolling checkpoint:                            │
│    │    │    │    <output>.ckpt.csv (18K det + 500 LLM rows)         │
│    │    │    └─ ✅ Batch complete                                    │
│    │    │                                                           │
│    │    ├─ LLM Batch 2 (rows 501–1000 of llm_queue)                 │
│    │    │    ├─ Process 500 rows                                    │
│    │    │    ├─ Overwrite checkpoint (18K + 1000 rows)               │
│    │    │    └─ ✅ Batch complete                                    │
│    │    │                                                           │
│    │    ├─ ...                                                      │
│    │    │                                                           │
│    │    ├─ 💥 CRASH at LLM row 10,042 (during batch 21)             │
│    │    │                                                           │
│    │    │  Recovery:                                                │
│    │    │    python -m src.batch_runner input.csv --resume           │
│    │    │    1. Load checkpoint → 18K det + 10K LLM = 28K done      │
│    │    │    2. Pass 1 re-runs but skips checkpointed rows           │
│    │    │    3. Pass 2 processes only remaining ~4K LLM rows         │
│    │    │    4. Write final output, delete checkpoint                │
│    │    │                                                           │
│    │    └─ LLM Batch 28 (rows 13,501–14,000)                        │
│    │         ├─ Write final output: <output>.csv (32,000 rows)       │
│    │         ├─ Delete checkpoint file                               │
│    │         └─ ✅ Job complete                                      │
└─────────────────────────────────────────────────────────────────────┘
```

#### Checkpoint file format

The checkpoint is a CSV with all the standard output columns **plus** a `__row_index__` column (1-based, matches the row's position in the input file). On resume, each `__row_index__` is matched to the input to determine which rows are already done.

#### Usage

```bash
# Normal run — checkpoints are written automatically between batches
python -m src.batch_runner data/input/big.csv -o data/output/big_output.csv

# Resume after crash — skips completed rows
python -m src.batch_runner data/input/big.csv -o data/output/big_output.csv --resume

# Via shell script
./scripts/run_batch.sh data/input/big.csv --resume -o data/output/big_output.csv
```

#### Key behaviors

| Behavior | Detail |
|----------|--------|
| Checkpoint frequency | After every batch (controlled by `--batch-size`, default 200) |
| Checkpoint file | `<output_path>.ckpt.csv` — single rolling file, overwritten each batch |
| Resume detection | `--resume` flag loads checkpoint, marks matching `__row_index__` rows as done |
| Batch skipping | Entire batches are skipped if all their rows are in the checkpoint |
| Partial batch | If a batch is partially done (crash mid-batch), un-done rows are re-processed |
| Max data loss | ≤ 1 batch_size of rows (default 200). Worst case: crash right before checkpoint write. |
| Cleanup | Checkpoint file is deleted after successful final output write |
| Type restoration | `confidence_score`, `geonames_id`, `geonames_match`, `mismatch_detected` are restored to proper Python types on resume |

### 10.2 Recommended Settings for 32K Rows

```bash
# 32K rows with Ollama (OLLAMA_NUM_PARALLEL=4):
python -m src.batch_runner data/input/addresses_32k.csv \
    -o data/output/addresses_32k_output.csv \
    --concurrency 8 \
    --batch-size 500

# If it crashes:
python -m src.batch_runner data/input/addresses_32k.csv \
    -o data/output/addresses_32k_output.csv \
    --concurrency 8 \
    --batch-size 500 \
    --resume
```

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `--concurrency 8` | 8 rows in-flight | Saturates 4 Ollama slots + 4 deterministic rows |
| `--batch-size 500` | 500 rows per checkpoint | ~10 min between checkpoints at ~1 row/sec. Max loss: 500 rows (~8 min). |
| `LLM_CONCURRENCY=4` | 4 parallel LLM calls | Match `OLLAMA_NUM_PARALLEL=4` on the server |

Estimated runtime with two-pass architecture:
- **Pass 1 (deterministic):** 32K rows × ~3 ms each = ~96 seconds
- **Pass 2 (LLM):** 32K × ~43% unresolved × 10 s each / 4 parallel ≈ **9.5 hours** (local Ollama)
- **Pass 2 (LLM):** 32K × ~43% unresolved × 0.5 s each / 8 parallel ≈ **5 minutes** (cloud Gemini Flash)

> **Note:** The LLM ratio depends on data quality. The 13-row adversarial test set shows 54% LLM; real production data with well-formed addresses typically shows 15–30% LLM.

### 10.3 Crash Scenarios & Recovery (Local)

| Scenario | Data Loss | Recovery |
|----------|-----------|----------|
| Process killed (Ctrl+C, OOM, power loss) | ≤ batch_size rows (default 200) | `--resume` loads last checkpoint, re-processes remaining rows |
| Ollama crash/timeout | 1 row (marked `rejected`) | Row gets `status=rejected, review_reason=unhandled_exception`. Other rows in batch continue. |
| Disk full during checkpoint write | ≤ batch_size rows | Previous checkpoint may be corrupt. Delete `.ckpt.csv` and restart without `--resume`. |
| Input file changed between runs | Undefined | `--resume` matches by `__row_index__` (position). If rows shifted, results will be misaligned. Always use same input file. |

### 10.4 Production Checkpointing (Dataflow — Future)

> The following sections describe the **planned production design** for GCP Dataflow. Not yet implemented — target for Phase 9/10.

For production batch processing (millions of rows via GCP Dataflow), the checkpointing strategy uses Cloud SQL + GCS instead of a local CSV:

```
Checkpointing layer (Cloud SQL)      ADK layer (InMemorySessionService)
┌───────────────────────┐           ┌───────────────────────────────┐
│  Job: abc              │           │  Session: row_42              │
│  processed_rows: 3000  │           │  state: {address_1, town...}  │
│  chunk_003 committed   │  wraps →  │  Lifecycle: create → run → GC │
│                        │           │  No persistence needed        │
└───────────────────────┘           └───────────────────────────────┘
```

**Key differences from local mode:**
- Progress tracked in Cloud SQL (durable, queryable)
- Partial results written to GCS as chunk CSVs
- Dataflow handles worker-level retries automatically
- Application-level checkpointing handles job-level restart

#### 10.4.1 Idempotency (Production)

| Component | Idempotency Mechanism |
|-----------|----------------------|
| Cloud SQL results | `UNIQUE (job_id, row_index)` constraint + `ON CONFLICT DO UPDATE` (upsert) |
| GCS partial CSVs | Overwrite same chunk filename (GCS write is atomic per object) |
| Job progress | `UPDATE` is naturally idempotent |
| LLM calls | Deterministic (temperature=0) + response cache in Redis |
| ADK session state | `InMemorySessionService` — fresh per row, no stale state |

#### 10.4.2 Monitoring Checkpoints (Production)

| Metric | Purpose |
|--------|---------|
| `address_checkpoint_committed_total` | Track checkpointing frequency |
| `address_checkpoint_duration_seconds` | Detect slow commits (DB bottleneck) |
| `address_job_resume_total` | How often jobs are resumed (crash frequency indicator) |
| `address_chunk_reprocess_total` | Rows re-processed after resume (waste indicator) |

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

### ADR-002: Two-Pass Hybrid Architecture for Batch Processing

**Date:** 20 February 2026
**Status:** Implemented

**Context:**
The single-pass batch runner created an ADK session for *every* row — even the ~57% that resolve deterministically in <5 ms. Per-row ADK overhead (session creation, event streaming, state-delta computation, 4 sub-agent invocations) added ~50–200 ms per row. At scale (30M rows × 57% deterministic × 100 ms), this wastes ~475 hours of pure framework overhead. A 32K-row batch took >6.5 hours; the old non-agentic pipeline (`src/pipeline.py`) was faster because it ran deterministic steps as plain function calls and only batched unresolved rows to the LLM.

**Decision:**
Implement a two-pass hybrid architecture in `src/batch_runner.py`:

- **Pass 1 — Deterministic (pure Python, no ADK):** Run Steps 0–5 + 7 + 8 as direct `services/` function calls for every row. Resolved rows go straight to output. Unresolved rows are collected into an LLM queue.
- **Pass 2 — LLM only (ADK sessions):** Only create ADK sessions for unresolved rows. Inject the pre-computed state from Pass 1, so the `DeterministicResolverAgent` detects pre-computation (via `status=unresolved` + `raw_address` populated) and skips. The orchestrator routes directly to LLM → revalidation → persist.

**Rationale:**
- Deterministic rows cost ~1–5 ms each (direct function calls) instead of ~50–200 ms (ADK session)
- LLM rows are unchanged — identical ADK pipeline, identical tool-calling, identical quality
- All `services/` functions are already plain Python with no ADK dependency — the architecture was designed for this (§15 benefit #10)
- `adk web` and `adk api_server` remain full single-pass ADK (no change)

**Consequences:**
- (+) 32K rows: Pass 1 completes in ~30 seconds (vs. ~30–60 minutes of ADK overhead previously)
- (+) 30M rows: deterministic pass completes in minutes instead of ~475 hours of overhead
- (+) Zero quality compromise — identical service functions, identical LLM tool-calling
- (+) Checkpointing remains fully functional (checkpoints include both deterministic and LLM results)
- (+) `adk web` / `adk api_server` unaffected — they still use the full ADK pipeline for single addresses
- (–) Two code paths: deterministic steps run both as direct calls (batch) and via sub-agent (adk web). Mitigation: both call the same `services/` functions — the sub-agent is a thin wrapper.
- (–) `DeterministicResolverAgent` needs a fast-exit check for pre-computed state. Mitigation: 5-line guard clause.

---

## 18. Tuning Parameters & LLM Cost Reduction

The pipeline's LLM ratio (percentage of rows sent to the LLM) is the primary cost driver. All the mechanisms to reduce it are **already implemented** — the remaining work is threshold tuning against production data.

### 18.1 Configurable Parameters

All parameters are set via environment variables (`.env` file) with sensible defaults:

| Parameter | Default | Range | Effect | Source |
|-----------|---------|-------|--------|--------|
| `FUZZY_MATCH_THRESHOLD` | 92 | 50–100 | Minimum RapidFuzz score to accept a scan match (Step 5). Lower → more matches, higher false-positive risk. | `utils/config.py` |
| `FUZZY_AMBIGUITY_MARGIN` | 5 | 1–50 | Minimum score gap between #1 and #2 fuzzy candidates. Prevents ambiguous matches (e.g., "Bari" vs "Barr"). | `utils/config.py` |
| `CONFIDENCE_EXACT_PRIMARY` | 1.00 | 0–1 | Confidence assigned to exact match on primary/ASCII city name. | `utils/config.py` |
| `CONFIDENCE_EXACT_ALTERNATE` | 0.95 | 0–1 | Confidence assigned to exact match on alternate name. | `utils/config.py` |
| `CONFIDENCE_FUZZY_SCAN` | 0.80 | 0–1 | Confidence assigned to fuzzy scan matches (Step 5). | `utils/config.py` |
| `CONFIDENCE_LLM_CONFIRMED` | 0.75 | 0–1 | Confidence when LLM result is verified by GeoNames exact match. | `utils/config.py` |
| `CONFIDENCE_LLM_FUZZY_CONFIRMED` | 0.70 | 0–1 | Confidence when LLM result is verified by fuzzy match only. | `utils/config.py` |
| `CONFIDENCE_LLM_UNVERIFIED` | 0.40 | 0–1 | Confidence when LLM result cannot be verified in GeoNames at all. | `utils/config.py` |

### 18.2 How Each Pipeline Step Reduces LLM Usage

The deterministic resolver (Steps 0–5) has four mechanisms to resolve a row before it reaches the LLM:

| Step | Mechanism | Implementation | What It Catches |
|------|-----------|----------------|-----------------|
| **Step 2** | Postal code → city hint | `services/postal_lookup.py` queries `postal_codes` table → sets `postal_town_candidate` and `postal_admin1_code` | Addresses with valid postal codes get a city candidate even when libpostal fails to extract one |
| **Step 3** | Exact match with disambiguation | `services/geonames_exact.py` tries `libpostal_town` then `postal_town_candidate` against 230K+ city name variants. Disambiguates via admin1 code from Step 2. | Clean city names, postal-code-resolved cities, alternate names (Mumbai/Bombay) |
| **Step 4** | Country-code mismatch detection | `services/mismatch_detector.py` checks if the city exists in *any* country when not found in the stated one → sets `suggested_country_code` | "Barisardo" tagged as Ireland → detected as Italy |
| **Step 5** | Fuzzy n-gram scan | `services/address_scanner.py` scans raw address text against all city names for the country. Phase 1: exact n-gram matching. Phase 2: RapidFuzz fuzzy matching with ambiguity guards. | Misspellings ("Dbulin" → Dublin), city names embedded in unstructured text |

### 18.3 GeoNames Database Coverage

The GeoNames SQLite database (`data/database/geonames.db`) is built by `src/geonames_etl.py` from three source files:

| Source File | Records | What It Provides |
|-------------|---------|------------------|
| `cities500.txt` (pop ≥ 500) | 229,680 cities | Primary city lookup + alternate names (1.1M name variants) |
| `allCountries.txt` | 1,826,619 postal codes | Postal code → city + admin1 region mapping |
| `admin1CodesASCII.txt` | 3,862 admin1 regions | Admin1 code → region name mapping (for disambiguation) |

Total database size: **329 MB**.

To rebuild after changing the source file:

```bash
python -m src.geonames_etl              # Rebuilds data/database/geonames.db
python -m src.geonames_etl --db /path.db # Custom output path
```

### 18.4 Tuning Strategy

Recommended approach for production threshold tuning:

1. **Baseline:** Run the 32K-row batch with default settings (`FUZZY_MATCH_THRESHOLD=92`, `FUZZY_AMBIGUITY_MARGIN=5`). Record the LLM ratio from the BATCH SUMMARY log.

2. **Lower threshold experiment:** Set `FUZZY_MATCH_THRESHOLD=88` in `.env` and re-run. Compare:
   - Did the LLM ratio drop? (Good — more rows resolved deterministically)
   - Did any previously-correct rows get wrong cities? (Bad — threshold too low)

3. **Tighten ambiguity margin:** If false positives appear, raise `FUZZY_AMBIGUITY_MARGIN` from 5 to 8. This rejects matches where the top two candidates are too close.

4. **Validate:** Spot-check the output CSV for rows where `parser_source=geonames_scan` — these are the fuzzy-matched rows most sensitive to threshold changes.

5. **Production target:** Aim for **15–30% LLM ratio** (down from 54% on the 13-row adversarial test set). Real production data with well-formed addresses should naturally have a much lower LLM ratio than our deliberately adversarial test set.

> **Key insight:** The 13-row test file is intentionally adversarial — wrong countries, person names instead of addresses, minimal data. Production data with real addresses will have a significantly higher deterministic resolution rate.

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
