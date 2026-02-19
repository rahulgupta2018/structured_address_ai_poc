# Structured Address AI v3 — ADK Agentic Pipeline Architecture

> **Version:** 3.0 — _February 2026_
> **Status:** Proposed — Refactors v2 pipeline as a Google ADK agent workflow
> **Prerequisite:** [DESIGN_V2.md](./DESIGN_V2.md) — production infrastructure, data architecture, security, cost model
> **Audience:** Engineering, Architecture Review

---

## Table of Contents

1. [Purpose & Scope](#1-purpose--scope)
2. [Key Insight: The Entire Pipeline is an Agent Workflow](#2-key-insight-the-entire-pipeline-is-an-agent-workflow)
3. [ADK Agent Taxonomy Recap](#3-adk-agent-taxonomy-recap)
4. [Pipeline as Agents — Mapping](#4-pipeline-as-agents--mapping)
5. [Architecture Diagram](#5-architecture-diagram)
6. [Agent Definitions](#6-agent-definitions)
7. [Custom Orchestrator: AddressPipelineAgent](#7-custom-orchestrator-addresspipelineagent)
8. [Session State Contract](#8-session-state-contract)
9. [ADK Runtime Modes](#9-adk-runtime-modes)
10. [Observability — Free from ADK](#10-observability--free-from-adk)
11. [Evaluation Framework](#11-evaluation-framework)
12. [Deployment Matrix](#12-deployment-matrix)
13. [Migration from v2 Design](#13-migration-from-v2-design)
14. [Benefits Summary](#14-benefits-summary)
15. [Risks & Mitigations](#15-risks--mitigations)
16. [Decision Record](#16-decision-record)

---

## 1. Purpose & Scope

### What This Document Covers

DESIGN_V2.md defines the full production system: infrastructure (GCP Dataflow, Cloud SQL, Redis, GCS), data architecture (GeoNames ETL, PostgreSQL schema), security, compliance, cost model, and the 8-step processing pipeline.

**This document (v3) redesigns the pipeline orchestration layer** — how the 8 steps are wired together and executed — using Google ADK (Agent Development Kit). Everything else in v2 remains unchanged.

### What Changed

| Aspect | v2 Design | v3 Design |
|--------|-----------|-----------|
| Pipeline orchestration | Custom Python `pipeline.py` with procedural waterfall | **ADK CustomAgent** (`AddressPipelineAgent`) orchestrating 8 sub-agents |
| Step 6 (LLM) | ADK `LlmAgent` with tools — unchanged from v2 §9.6 | Same — but now a first-class sub-agent within the workflow |
| Steps 0–5, 7–8 | Custom Python functions | **ADK CustomAgents** (deterministic, no LLM) using `BaseAgent` |
| Runtime | Custom FastAPI + Dataflow integration | **ADK Runtime**: `adk web` (dev), `adk api_server` (API), `adk run` (CLI) |
| Observability | Custom OpenTelemetry + Prometheus | ADK built-in event tracing + OpenTelemetry (additive) |
| Dev experience | Run pytest, hit API manually | `adk web` → browser UI with trace inspection, tool call visualization |
| Evaluation | Custom benchmark scripts | ADK built-in evaluation framework + custom benchmark |

### What Stays from v2 (Unchanged)

- GCP infrastructure (Dataflow, Cloud SQL, Memorystore Redis, GCS)
- GeoNames data architecture (PostgreSQL schema, ETL, cities + postal codes + admin1)
- Security & compliance controls
- Cost model & scaling targets
- The 8-step pipeline logic itself — only the wiring changes

---

## 2. Key Insight: The Entire Pipeline is an Agent Workflow

The v2 pipeline has 9 steps (0–8). In v2, we designed only Step 6 as an ADK `LlmAgent`. But reading the ADK documentation reveals a more powerful pattern:

> **Workflow agents operate based on predefined logic. They determine the execution sequence according to their type (sequential, parallel, loop) without consulting an LLM for the orchestration itself. This results in deterministic and predictable execution patterns.**
>
> — [ADK Workflow Agents Documentation](https://google.github.io/adk-docs/agents/workflow-agents/)

This means:

| ADK Agent Type | Maps To | LLM Required? |
|----------------|---------|---------------|
| **LlmAgent** | Step 6 (agentic LLM fallback) | ✅ Yes — this is the reasoning agent |
| **CustomAgent** (extends `BaseAgent`) | Steps 0–5, 7–8 (deterministic logic) | ❌ No — pure Python, no LLM |
| **CustomAgent** (orchestrator) | Top-level pipeline controller | ❌ No — conditional routing, state management |

**The entire 9-step pipeline becomes one ADK agent application.** Steps 0–5 and 7–8 are deterministic `CustomAgent`s. Step 6 is an `LlmAgent`. A top-level `CustomAgent` (the "orchestrator") wires them together with conditional logic (e.g., skip Step 6 if already resolved).

### Why This Matters

1. **Unified runtime** — `adk web`, `adk api_server`, and `adk run` work for the entire pipeline, not just the LLM step
2. **Full trace visibility** — every step emits ADK events, visible in `adk web` UI with a single trace per address row
3. **Built-in evaluation** — test the whole pipeline (not just the LLM) using ADK's evaluation framework
4. **Session state** — ADK manages the row's state (`ctx.session.state`) as it flows through steps; no custom dict-passing
5. **Same deployment** — deploys to Cloud Run, GKE, or Agent Engine as a single unit

---

## 3. ADK Agent Taxonomy Recap

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
| `LlmAgent` | LLM (Gemini, Ollama via LiteLLM) | Non-deterministic | Step 6: agentic address parsing |
| `SequentialAgent` | Predefined order | Deterministic | Not directly used — our flow has conditionals |
| `ParallelAgent` | Run sub-agents concurrently | Deterministic | Could parallelize Steps 2+3 in future |
| `LoopAgent` | Repeat until condition | Deterministic | Not needed currently |
| `CustomAgent` | Your Python code in `_run_async_impl` | Your choice | Steps 0–5, 7–8 + the orchestrator |

We use **`CustomAgent`** (not `SequentialAgent`) for the orchestrator because our pipeline has **conditional branching**: if a row is resolved at Step 3, we skip Steps 4–6 entirely. `SequentialAgent` would run all steps unconditionally.

---

## 4. Pipeline as Agents — Mapping

### v2 Pipeline Steps → ADK Agents

| Step | v2 Name | ADK Agent Type | Agent Name | LLM? | Description |
|------|---------|---------------|------------|------|-------------|
| 0 | Schema Validation & Preprocessing | `CustomAgent` | `PreprocessAgent` | ❌ | Pydantic validation, NFKC normalization, postal code extraction |
| 1 | libpostal Parse | `CustomAgent` | `LibpostalParseAgent` | ❌ | Extract town, street, building, postal_code, state |
| 2 | Postal Code Cross-Reference | `CustomAgent` | `PostalCodeLookupAgent` | ❌ | Postal code → region + city. Strong disambiguation signal |
| 3 | GeoNames Exact Validation | `CustomAgent` | `GeoNamesExactAgent` | ❌ | Exact match with disambiguation (postal, admin1, population) |
| 4 | Country-Code Mismatch Detection | `CustomAgent` | `MismatchDetectAgent` | ❌ | Cross-validate country_code against address signals |
| 5 | GeoNames Raw-Address Scan | `CustomAgent` | `GeoNamesScanAgent` | ❌ | Fuzzy scan of raw address text against city lexicon |
| 6 | Agentic LLM Fallback | **`LlmAgent`** | `LlmAddressParserAgent` | ✅ | Agentic reasoning with GeoNames tools |
| 7 | Final Re-Validation | `CustomAgent` | `RevalidationAgent` | ❌ | Safety-net GeoNames check on LLM output |
| 8 | Persist & Output | `CustomAgent` | `PersistAgent` | ❌ | Write to Cloud SQL + GCS |
| — | **Orchestrator** | **`CustomAgent`** | **`AddressPipelineAgent`** | ❌ | Conditional routing: skip LLM if resolved early |

### Agent Hierarchy

```
AddressPipelineAgent (CustomAgent — orchestrator)
  ├── PreprocessAgent (CustomAgent)
  ├── LibpostalParseAgent (CustomAgent)
  ├── PostalCodeLookupAgent (CustomAgent)
  ├── GeoNamesExactAgent (CustomAgent)
  ├── MismatchDetectAgent (CustomAgent)
  ├── GeoNamesScanAgent (CustomAgent)
  ├── LlmAddressParserAgent (LlmAgent)      ← only agent that uses LLM
  ├── RevalidationAgent (CustomAgent)
  └── PersistAgent (CustomAgent)
```

---

## 5. Architecture Diagram

### Single-Row Flow Through the Agent Pipeline

```
User Input (address row via adk web / adk api_server / adk run)
  │
  ▼
┌─────────────────────────────────────────────────────────────────┐
│  AddressPipelineAgent (CustomAgent — _run_async_impl)           │
│                                                                  │
│  session.state = {                                               │
│    "address_1": "Via Roma 15",                                   │
│    "address_2": "08042 Barisardo (OG)",                          │
│    "country_code": "IE",                                         │
│    "status": "pending"                                           │
│  }                                                               │
│                                                                  │
│  ┌──────────────┐                                                │
│  │ Step 0       │  PreprocessAgent                               │
│  │ Preprocess   │──→ state: normalized_address, extracted_postal │
│  └──────┬───────┘                                                │
│         ▼                                                        │
│  ┌──────────────┐                                                │
│  │ Step 1       │  LibpostalParseAgent                           │
│  │ libpostal    │──→ state: town_candidate, street, postal_code  │
│  └──────┬───────┘                                                │
│         ▼                                                        │
│  ┌──────────────┐                                                │
│  │ Step 2       │  PostalCodeLookupAgent                         │
│  │ Postal Xref  │──→ state: postal_region, postal_city_hint     │
│  └──────┬───────┘                                                │
│         ▼                                                        │
│  ┌──────────────┐                                                │
│  │ Step 3       │  GeoNamesExactAgent                            │
│  │ Exact Match  │──→ state: status=validated? or unresolved      │
│  └──────┬───────┘                                                │
│         │                                                        │
│         ├── if status == "validated" ──→ SKIP to Step 7 ────┐    │
│         │                                                   │    │
│         ▼                                                   │    │
│  ┌──────────────┐                                           │    │
│  │ Step 4       │  MismatchDetectAgent                      │    │
│  │ Mismatch     │──→ state: mismatch_warning, suggested_cc  │    │
│  └──────┬───────┘                                           │    │
│         ▼                                                   │    │
│  ┌──────────────┐                                           │    │
│  │ Step 5       │  GeoNamesScanAgent                        │    │
│  │ Fuzzy Scan   │──→ state: status=validated? or unresolved │    │
│  └──────┬───────┘                                           │    │
│         │                                                   │    │
│         ├── if status == "validated" ──→ SKIP to Step 7 ──┐ │    │
│         │                                                 │ │    │
│         ▼                                                 │ │    │
│  ┌──────────────┐                                         │ │    │
│  │ Step 6       │  LlmAddressParserAgent (LlmAgent)       │ │    │
│  │ Agentic LLM  │──→ state: llm_result, tool_calls_log    │ │    │
│  │ ✨ USES LLM  │  Tools: query_city, query_postal_code,  │ │    │
│  │              │  query_admin1, search_city_fuzzy,         │ │    │
│  │              │  list_countries_for_city                  │ │    │
│  └──────┬───────┘                                         │ │    │
│         ▼                                                 │ │    │
│  ┌──────────────┐  ◄──────────────────────────────────────┘─┘    │
│  │ Step 7       │  RevalidationAgent                             │
│  │ Re-Validate  │──→ state: final status, confidence_score       │
│  └──────┬───────┘                                                │
│         ▼                                                        │
│  ┌──────────────┐                                                │
│  │ Step 8       │  PersistAgent                                  │
│  │ Persist      │──→ Cloud SQL + GCS output                      │
│  └──────────────┘                                                │
│                                                                  │
│  Output: session.state["final_result"]                           │
└─────────────────────────────────────────────────────────────────┘
```

### Key Design Decisions in the Flow

1. **Early exit** — If Step 3 (exact match) resolves the row, Steps 4–6 are skipped entirely. This saves LLM cost on ~85% of rows.
2. **Conditional Step 6** — Only rows that reach Step 5 and remain unresolved are dispatched to the LLM agent.
3. **Step 7 always runs** — Even for rows resolved at Step 3, re-validation provides a safety net (defense in depth).
4. **All state in `ctx.session.state`** — No custom dict-passing. ADK manages it.

---

## 6. Agent Definitions

### 6.1 Deterministic Agents (CustomAgent — No LLM)

Each deterministic agent follows the same pattern: read from `ctx.session.state`, execute pure Python logic, write results back to `ctx.session.state`.

```python
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from typing import AsyncGenerator
from typing_extensions import override


class PreprocessAgent(BaseAgent):
    """Step 0: Schema validation, NFKC normalization, postal code extraction."""

    model_config = {"arbitrary_types_allowed": True}

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        # Read raw input from session state
        address_1 = state.get("address_1", "")
        address_2 = state.get("address_2", "")
        address_3 = state.get("address_3", "")
        country_code = state.get("country_code", "")

        # Normalize (NFKC, casefold, whitespace collapse)
        normalized = normalize_address(address_1, address_2, address_3)
        state["normalized_address"] = normalized

        # Extract postal code from raw text
        extracted_postal = extract_postal_code(normalized, country_code)
        state["extracted_postal_code"] = extracted_postal

        # Validate schema
        validation_errors = validate_input_schema(state)
        state["validation_errors"] = validation_errors
        state["step_0_complete"] = True

        yield Event(author=self.name, content=None)  # signal completion


class LibpostalParseAgent(BaseAgent):
    """Step 1: Parse address using libpostal. Extract town, street, building."""

    model_config = {"arbitrary_types_allowed": True}

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        parsed = parse_with_libpostal(state["normalized_address"])
        state["town_candidate"] = parsed.get("city", "")
        state["street"] = parsed.get("road", "")
        state["building"] = parsed.get("house_number", "")
        state["libpostal_postal_code"] = parsed.get("postcode", "")
        state["libpostal_state"] = parsed.get("state", "")
        state["step_1_complete"] = True

        yield Event(author=self.name, content=None)


class PostalCodeLookupAgent(BaseAgent):
    """Step 2: Cross-reference postal code against GeoNames postal codes DB."""

    model_config = {"arbitrary_types_allowed": True}

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        postal_code = state.get("extracted_postal_code") or state.get("libpostal_postal_code")
        country_code = state["country_code"]

        if postal_code:
            lookup = lookup_postal_code(postal_code, country_code)
            state["postal_region"] = lookup.get("admin1_name")
            state["postal_city_hint"] = lookup.get("place_name")
            state["postal_admin1_code"] = lookup.get("admin1_code")
        else:
            state["postal_region"] = None
            state["postal_city_hint"] = None

        state["step_2_complete"] = True
        yield Event(author=self.name, content=None)


class GeoNamesExactAgent(BaseAgent):
    """Step 3: Exact match town_candidate against GeoNames with disambiguation."""

    model_config = {"arbitrary_types_allowed": True}

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        town_candidate = state.get("town_candidate", "")
        country_code = state["country_code"]

        if not town_candidate:
            state["exact_match_result"] = None
            state["step_3_resolved"] = False
        else:
            match = exact_match_with_disambiguation(
                town=town_candidate,
                country_code=country_code,
                postal_region=state.get("postal_region"),
                postal_city_hint=state.get("postal_city_hint"),
                libpostal_state=state.get("libpostal_state"),
            )
            state["exact_match_result"] = match
            if match and match["confidence"] >= 0.80:
                state["status"] = "validated"
                state["resolved_town"] = match["name"]
                state["parser_source"] = "libpostal"
                state["confidence_score"] = match["confidence"]
                state["geonames_id"] = match["geonameid"]
                state["step_3_resolved"] = True
            else:
                state["step_3_resolved"] = False

        state["step_3_complete"] = True
        yield Event(author=self.name, content=None)


class MismatchDetectAgent(BaseAgent):
    """Step 4: Detect country-code mismatches using address signals."""

    model_config = {"arbitrary_types_allowed": True}

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        mismatch = detect_country_mismatch(
            town_candidate=state.get("town_candidate", ""),
            country_code=state["country_code"],
            postal_code=state.get("extracted_postal_code"),
            normalized_address=state["normalized_address"],
        )

        state["mismatch_detected"] = mismatch.get("detected", False)
        state["suggested_country_code"] = mismatch.get("suggested_cc")
        state["mismatch_signals"] = mismatch.get("signals", [])
        state["step_4_complete"] = True

        yield Event(author=self.name, content=None)


class GeoNamesScanAgent(BaseAgent):
    """Step 5: Fuzzy scan raw address text against GeoNames city lexicon."""

    model_config = {"arbitrary_types_allowed": True}

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        # Use suggested country if mismatch detected
        effective_cc = state.get("suggested_country_code") or state["country_code"]

        scan_result = scan_address_for_city(
            raw_address=state["normalized_address"],
            country_code=effective_cc,
            postal_region=state.get("postal_region"),
        )

        if scan_result and scan_result["confidence"] >= 0.70:
            state["status"] = "validated"
            state["resolved_town"] = scan_result["name"]
            state["parser_source"] = "geonames_scan"
            state["confidence_score"] = scan_result["confidence"]
            state["geonames_id"] = scan_result["geonameid"]
            state["step_5_resolved"] = True
        else:
            state["step_5_resolved"] = False

        state["step_5_complete"] = True
        yield Event(author=self.name, content=None)


class RevalidationAgent(BaseAgent):
    """Step 7: Re-validate resolved town against GeoNames (safety net)."""

    model_config = {"arbitrary_types_allowed": True}

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

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
                    revalidation["confidence"]
                )
            else:
                state["status"] = "needs_review"
                state["review_reason"] = "revalidation_failed"
        else:
            state["status"] = "needs_review" if state.get("town_candidate") else "rejected"
            state["review_reason"] = "no_town_resolved"

        state["step_7_complete"] = True
        yield Event(author=self.name, content=None)


class PersistAgent(BaseAgent):
    """Step 8: Persist results to Cloud SQL and GCS."""

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

        state["step_8_complete"] = True
        state["final_result"] = result
        yield Event(author=self.name, content=None)
```

### 6.2 LLM Agent — Step 6: `LlmAddressParserAgent`

This is the only agent that uses an LLM. It's an `LlmAgent` with 5 GeoNames-backed tools. Defined exactly as in DESIGN_V2.md §9.6, but now it's a proper sub-agent within the workflow.

```python
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm


# ── Tool Functions ──────────────────────────────────────────────
# ADK wraps these as FunctionTools automatically.
# Just provide type hints + docstrings — ADK generates the schema.

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
    Example: query_admin1('US', 'IL') → {'name': 'Illinois', 'code': 'US.IL'}
    Useful for mapping state abbreviations to full names."""
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


# ── System Prompt ───────────────────────────────────────────────

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
4. Return a JSON response with your structured output and reasoning.

Rules:
- If you cannot verify a town with tools, set status to "needs_review".
- Never fabricate a town name — only return names confirmed by tools.
- If you detect a country-code mismatch, include suggested_country_code.

Respond with ONLY a JSON object:
{
  "town": "verified city name",
  "street": "street from address",
  "building": "building number if present",
  "postal_code": "postal code if present",
  "status": "validated or needs_review",
  "suggested_country_code": "XX if mismatch, else null",
  "reasoning": "brief explanation of your reasoning"
}"""


# ── Agent Definition ────────────────────────────────────────────

from pydantic import BaseModel, Field

class LlmAddressOutput(BaseModel):
    """Structured output schema for the LLM agent."""
    town: str | None = Field(description="Verified city/town name from GeoNames")
    street: str | None = Field(description="Street address")
    building: str | None = Field(description="Building or house number")
    postal_code: str | None = Field(description="Postal/ZIP code")
    status: str = Field(description="validated or needs_review")
    suggested_country_code: str | None = Field(description="Corrected country code if mismatch")
    reasoning: str = Field(description="Brief explanation of the resolution")


# Production: Gemini via Vertex AI
llm_address_parser_agent = LlmAgent(
    name="LlmAddressParserAgent",
    model="gemini-2.0-flash",
    description="Parses unstructured addresses using LLM reasoning with GeoNames tool access.",
    instruction=SYSTEM_PROMPT,
    tools=[query_city, query_postal_code, query_admin1,
           search_city_fuzzy, list_countries_for_city],
    output_schema=LlmAddressOutput,
    output_key="llm_result",  # stored in session.state["llm_result"]
    generate_content_config=types.GenerateContentConfig(
        temperature=0.0,          # deterministic output
        max_output_tokens=500,    # bounded response
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

**Key ADK features used:**
- `instruction` uses `{var}` template syntax — ADK auto-fills from `session.state` (e.g., `{town_candidate}`)
- `tools` are plain Python functions — ADK auto-wraps them as `FunctionTool`
- `output_schema` enforces structured JSON output via Pydantic
- `output_key="llm_result"` auto-stores the result in `session.state["llm_result"]`

---

## 7. Custom Orchestrator: `AddressPipelineAgent`

This is the heart of the redesign. A `CustomAgent` that implements `_run_async_impl` with conditional routing — exactly like the `StoryFlowAgent` example in the [ADK Custom Agents documentation](https://google.github.io/adk-docs/agents/custom-agents/).

```python
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from typing import AsyncGenerator
from typing_extensions import override
import logging

logger = logging.getLogger(__name__)


class AddressPipelineAgent(BaseAgent):
    """
    Top-level orchestrator for the address parsing pipeline.
    
    Implements the 9-step waterfall with conditional LLM bypass:
    - Steps 0–2: Always run (preprocessing + postal lookup)
    - Step 3: Exact match — if resolved, skip to Step 7
    - Steps 4–5: Mismatch detection + fuzzy scan — if resolved, skip to Step 7
    - Step 6: LLM agent — only for unresolved rows (~15%)
    - Step 7: Re-validation — always runs (safety net)
    - Step 8: Persist — always runs
    """

    # ── Sub-agent declarations (Pydantic fields) ──
    preprocess_agent: PreprocessAgent
    libpostal_agent: LibpostalParseAgent
    postal_lookup_agent: PostalCodeLookupAgent
    exact_match_agent: GeoNamesExactAgent
    mismatch_agent: MismatchDetectAgent
    scan_agent: GeoNamesScanAgent
    llm_agent: LlmAgent                    # Step 6 — the only LLM-powered agent
    revalidation_agent: RevalidationAgent
    persist_agent: PersistAgent

    model_config = {"arbitrary_types_allowed": True}

    def __init__(
        self,
        name: str,
        preprocess_agent: PreprocessAgent,
        libpostal_agent: LibpostalParseAgent,
        postal_lookup_agent: PostalCodeLookupAgent,
        exact_match_agent: GeoNamesExactAgent,
        mismatch_agent: MismatchDetectAgent,
        scan_agent: GeoNamesScanAgent,
        llm_agent: LlmAgent,
        revalidation_agent: RevalidationAgent,
        persist_agent: PersistAgent,
    ):
        # Tell ADK framework about all sub-agents
        sub_agents_list = [
            preprocess_agent,
            libpostal_agent,
            postal_lookup_agent,
            exact_match_agent,
            mismatch_agent,
            scan_agent,
            llm_agent,
            revalidation_agent,
            persist_agent,
        ]

        super().__init__(
            name=name,
            preprocess_agent=preprocess_agent,
            libpostal_agent=libpostal_agent,
            postal_lookup_agent=postal_lookup_agent,
            exact_match_agent=exact_match_agent,
            mismatch_agent=mismatch_agent,
            scan_agent=scan_agent,
            llm_agent=llm_agent,
            revalidation_agent=revalidation_agent,
            persist_agent=persist_agent,
            sub_agents=sub_agents_list,
        )

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        """
        Orchestrates the 9-step pipeline with conditional LLM bypass.
        
        State flows through ctx.session.state — each sub-agent reads
        from and writes to it. No custom dict-passing needed.
        """

        logger.info(f"[{self.name}] Starting pipeline for row")

        # ── Step 0: Preprocess ──────────────────────────────────
        logger.info(f"[{self.name}] Step 0: Preprocessing")
        async for event in self.preprocess_agent.run_async(ctx):
            yield event

        if ctx.session.state.get("validation_errors"):
            logger.warning(f"[{self.name}] Validation errors — skipping to persist")
            ctx.session.state["status"] = "rejected"
            ctx.session.state["review_reason"] = "validation_error"
            async for event in self.persist_agent.run_async(ctx):
                yield event
            return

        # ── Step 1: libpostal Parse ─────────────────────────────
        logger.info(f"[{self.name}] Step 1: libpostal parse")
        async for event in self.libpostal_agent.run_async(ctx):
            yield event

        # ── Step 2: Postal Code Cross-Reference ─────────────────
        logger.info(f"[{self.name}] Step 2: Postal code lookup")
        async for event in self.postal_lookup_agent.run_async(ctx):
            yield event

        # ── Step 3: GeoNames Exact Match ────────────────────────
        logger.info(f"[{self.name}] Step 3: GeoNames exact match")
        async for event in self.exact_match_agent.run_async(ctx):
            yield event

        # ── CONDITIONAL: Skip to Step 7 if resolved ─────────────
        if ctx.session.state.get("step_3_resolved"):
            logger.info(f"[{self.name}] ✅ Resolved at Step 3 — skipping to re-validation")
            async for event in self.revalidation_agent.run_async(ctx):
                yield event
            async for event in self.persist_agent.run_async(ctx):
                yield event
            return

        # ── Step 4: Country-Code Mismatch Detection ─────────────
        logger.info(f"[{self.name}] Step 4: Mismatch detection")
        async for event in self.mismatch_agent.run_async(ctx):
            yield event

        # ── Step 5: GeoNames Raw-Address Scan ───────────────────
        logger.info(f"[{self.name}] Step 5: GeoNames fuzzy scan")
        async for event in self.scan_agent.run_async(ctx):
            yield event

        # ── CONDITIONAL: Skip to Step 7 if resolved ─────────────
        if ctx.session.state.get("step_5_resolved"):
            logger.info(f"[{self.name}] ✅ Resolved at Step 5 — skipping LLM")
            async for event in self.revalidation_agent.run_async(ctx):
                yield event
            async for event in self.persist_agent.run_async(ctx):
                yield event
            return

        # ── Step 6: Agentic LLM Fallback ───────────────────────
        logger.info(f"[{self.name}] Step 6: LLM agent (unresolved row)")
        async for event in self.llm_agent.run_async(ctx):
            yield event

        # Extract LLM result into standard state keys
        llm_result = ctx.session.state.get("llm_result")
        if llm_result:
            ctx.session.state["resolved_town"] = llm_result.get("town")
            ctx.session.state["parser_source"] = "llm_agent"
            if llm_result.get("suggested_country_code"):
                ctx.session.state["suggested_country_code"] = llm_result["suggested_country_code"]

        # ── Step 7: Re-Validation ───────────────────────────────
        logger.info(f"[{self.name}] Step 7: Re-validation")
        async for event in self.revalidation_agent.run_async(ctx):
            yield event

        # ── Step 8: Persist ─────────────────────────────────────
        logger.info(f"[{self.name}] Step 8: Persist results")
        async for event in self.persist_agent.run_async(ctx):
            yield event

        logger.info(f"[{self.name}] Pipeline complete — status: {ctx.session.state.get('status')}")
```

### Instantiation

```python
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# ── Create all sub-agents ──
preprocess = PreprocessAgent(name="PreprocessAgent")
libpostal = LibpostalParseAgent(name="LibpostalParseAgent")
postal_lookup = PostalCodeLookupAgent(name="PostalCodeLookupAgent")
exact_match = GeoNamesExactAgent(name="GeoNamesExactAgent")
mismatch = MismatchDetectAgent(name="MismatchDetectAgent")
scan = GeoNamesScanAgent(name="GeoNamesScanAgent")
revalidation = RevalidationAgent(name="RevalidationAgent")
persist = PersistAgent(name="PersistAgent")

# ── Create the orchestrator ──
pipeline_agent = AddressPipelineAgent(
    name="AddressPipelineAgent",
    preprocess_agent=preprocess,
    libpostal_agent=libpostal,
    postal_lookup_agent=postal_lookup,
    exact_match_agent=exact_match,
    mismatch_agent=mismatch,
    scan_agent=scan,
    llm_agent=llm_address_parser_agent,  # from §6.2
    revalidation_agent=revalidation,
    persist_agent=persist,
)

# ── Run via ADK Runner ──
async def process_address(address_row: dict):
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
        if event.is_final_response():
            pass  # results are in session.state["final_result"]

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

### 8.2 Intermediate Keys (Set by Agents)

| Key | Set By | Type | Description |
|-----|--------|------|-------------|
| `normalized_address` | Step 0 | `str` | NFKC-normalized, casefolded, whitespace-collapsed |
| `extracted_postal_code` | Step 0 | `str \| None` | Postal code extracted via regex |
| `validation_errors` | Step 0 | `list[str]` | Schema validation errors (empty = valid) |
| `town_candidate` | Step 1 | `str` | libpostal-extracted town name |
| `street` | Step 1 | `str` | libpostal-extracted street |
| `building` | Step 1 | `str` | libpostal-extracted building number |
| `libpostal_postal_code` | Step 1 | `str` | libpostal-extracted postal code |
| `libpostal_state` | Step 1 | `str` | libpostal-extracted state/province |
| `postal_region` | Step 2 | `str \| None` | Admin1 region name from postal code lookup |
| `postal_city_hint` | Step 2 | `str \| None` | City name hint from postal code |
| `postal_admin1_code` | Step 2 | `str \| None` | Admin1 code from postal code |
| `exact_match_result` | Step 3 | `dict \| None` | Full match record if found |
| `step_3_resolved` | Step 3 | `bool` | Whether exact match resolved the row |
| `mismatch_detected` | Step 4 | `bool` | Whether country-code mismatch was detected |
| `suggested_country_code` | Step 4 | `str \| None` | Corrected country code |
| `mismatch_signals` | Step 4 | `list[str]` | Signals that triggered mismatch |
| `step_5_resolved` | Step 5 | `bool` | Whether fuzzy scan resolved the row |
| `llm_result` | Step 6 | `dict` | LLM agent's structured output (auto-set via `output_key`) |

### 8.3 Output Keys (Final Result)

| Key | Set By | Type | Description |
|-----|--------|------|-------------|
| `status` | Steps 3/5/7 | `str` | `validated`, `needs_review`, or `rejected` |
| `resolved_town` | Steps 3/5/6 | `str \| None` | Final resolved town name |
| `parser_source` | Steps 3/5/6 | `str` | `libpostal`, `geonames_scan`, or `llm_agent` |
| `confidence_score` | Steps 3/5/7 | `float` | 0.00–1.00 |
| `geonames_id` | Steps 3/5 | `int \| None` | GeoNames ID of matched city |
| `review_reason` | Step 7 | `str \| None` | Why it's `needs_review` |
| `final_result` | Step 8 | `dict` | Complete result record for persistence |

### 8.4 State Template Injection

ADK's `{var}` syntax in `LlmAgent.instruction` auto-fills from `session.state`. The LLM agent's system prompt references:

```
- {town_candidate}           → from Step 1
- {country_code}             → from input
- {extracted_postal_code}    → from Step 0
- {postal_region}            → from Step 2
- {mismatch_detected}        → from Step 4
- {suggested_country_code}   → from Step 4
```

No manual prompt construction needed — ADK handles the injection.

---

## 9. ADK Runtime Modes

ADK provides three built-in runtimes. The **same agent code** runs in all three — zero code changes.

### 9.1 Dev Mode: `adk web`

```bash
# Start the dev UI
adk web --port 8000

# Opens browser at http://localhost:8000
# Features:
#  - Chat interface to send address rows
#  - Real-time event trace (see every step)
#  - Tool call visualization (see LLM's GeoNames queries)
#  - Session state inspection (see state after each step)
#  - Auto-reload on code changes
```

**Use case:** Development, debugging, demos. Not for production.

### 9.2 API Mode: `adk api_server`

```bash
# Start the REST API server
adk api_server --port 8000

# Endpoints:
# POST /apps/address_ai/users/{user_id}/sessions/{session_id}
#   → Create session with initial state (address row)
#
# POST /run
#   → Process address row, return all events
#   Body: { "appName": "address_ai", "userId": "batch",
#           "sessionId": "row_42", "newMessage": {...} }
#
# POST /run_sse
#   → Stream events as SSE (Server-Sent Events)
#
# GET /apps/address_ai/users/{user_id}/sessions/{session_id}
#   → Get session with final state (results)
```

**Use case:** Replaces the custom FastAPI API from v2. Cloud Run deployment.

### 9.3 CLI Mode: `adk run`

```bash
# Run from terminal (interactive)
adk run address_ai

# Use case: quick testing, CI pipelines
```

### 9.4 Batch Processing Integration

For batch processing (millions of rows via GCP Dataflow), we don't use `adk api_server` directly. Instead, we **embed the ADK Runner inside a Dataflow ParDo transform**:

```python
import apache_beam as beam
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

class ProcessAddressFn(beam.DoFn):
    """Dataflow ParDo that runs the ADK pipeline for each row."""

    def setup(self):
        """Called once per worker — initialize the agent."""
        self.pipeline_agent = create_pipeline_agent()  # from §7
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
            pass  # consume events

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

This means:
- **Same agent code** runs in `adk web` (dev), `adk api_server` (API), and Dataflow (batch)
- No pipeline logic duplication across runtime modes
- Agent behavior is identical regardless of how it's invoked

---

## 10. Observability — Free from ADK

### 10.1 Built-in Event Tracing

Every agent emits ADK `Event` objects. The `adk web` UI displays them as a trace:

```
Trace: AddressPipelineAgent (row_42)
  ├── PreprocessAgent (2ms)
  │     └─ state: normalized_address="via roma 15 08042 barisardo (og)"
  ├── LibpostalParseAgent (5ms)
  │     └─ state: town_candidate="barisardo"
  ├── PostalCodeLookupAgent (1ms)
  │     └─ state: postal_region=null (postal code 08042 not found in IE)
  ├── GeoNamesExactAgent (3ms)
  │     └─ state: step_3_resolved=false (barisardo not in IE)
  ├── MismatchDetectAgent (2ms)
  │     └─ state: mismatch_detected=true, suggested_cc="IT"
  ├── GeoNamesScanAgent (4ms)
  │     └─ state: step_5_resolved=false
  ├── LlmAddressParserAgent (2400ms) ← LLM agent
  │     ├─ tool_call: query_city("barisardo", "IE") → []
  │     ├─ tool_call: list_countries_for_city("barisardo") → [{IT}]
  │     ├─ tool_call: query_postal_code("08042", "IT") → [{Bari Sardo}]
  │     └─ state: llm_result={town: "Barisardo", status: "validated", ...}
  ├── RevalidationAgent (3ms)
  │     └─ state: status="validated", confidence=0.75
  └── PersistAgent (10ms)
        └─ state: final_result={...}
  Total: ~2430ms
```

**In `adk web`**, this trace is rendered as an interactive timeline with expandable tool calls, state diffs, and latency breakdown — **with zero custom code**.

### 10.2 OpenTelemetry Integration

ADK supports [OpenTelemetry callbacks](https://google.github.io/adk-docs/callbacks/) for production observability. We add this on top of ADK's built-in tracing:

```python
from google.adk.agents import BaseAgent

# ADK callbacks integrate with third-party observability:
# - Comet Opik (natively supports ADK)
# - Any OpenTelemetry-compatible backend (Jaeger, Datadog, etc.)
```

The v2 Prometheus metrics (§13.1) and Grafana dashboards (§13.4) remain unchanged — they're infrastructure-level. ADK adds **agent-level** visibility on top.

### 10.3 What ADK Observability Gives Us (vs. v2)

| Capability | v2 (Custom) | v3 (ADK) |
|-----------|-------------|----------|
| Per-step latency | Custom spans with OpenTelemetry | ✅ Auto-generated from agent events |
| Tool call inspection | Custom logging | ✅ Built into `adk web` UI |
| Session state diff | Not available | ✅ State viewer in `adk web` |
| LLM prompt/response viewing | Custom debug logging | ✅ Full prompt trace in UI |
| Step-by-step replay | Not available | ✅ Event history in session |
| Production tracing | OpenTelemetry (keep) | OpenTelemetry + ADK events |

---

## 11. Evaluation Framework

ADK includes a [built-in evaluation framework](https://google.github.io/adk-docs/evaluate/) that we can use alongside our custom benchmark (v2 §15.3).

### 11.1 ADK Evaluation

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

### 11.2 Evaluation Criteria

| Criterion | Description | Target |
|-----------|-------------|--------|
| **Town accuracy** | Does `resolved_town` match `expected_town`? | ≥ 95% |
| **Status correctness** | Does final `status` match expected? | ≥ 98% |
| **Mismatch detection** | Are wrong country codes flagged? | ≥ 90% |
| **False positive rate** | Wrong town marked `validated` | 0% (hard gate) |
| **LLM skip rate** | % of rows resolved without LLM | ≥ 80% |
| **End-to-end latency** | Deterministic path p95 | < 500ms |
| **LLM path latency** | LLM path p95 | < 5s |

### 11.3 Custom Benchmark (from v2 §15.3)

The 500+ curated addresses with expected results run as a regression suite. With ADK, this integrates directly:

```bash
# Run evaluation using ADK's framework
adk eval --agent address_ai --dataset tests/benchmark/eval_dataset.json

# Or via pytest (existing v2 tests)
pytest tests/test_pipeline_e2e.py -v
```

---

## 12. Deployment Matrix

### 12.1 Environment → Runtime Mapping

| Environment | ADK Runtime | LLM Provider | Processing Mode | Notes |
|-------------|-------------|-------------|-----------------|-------|
| **Local dev** | `adk web` | Ollama (via LiteLLM) | Single-row interactive | `adk web` for trace inspection |
| **Local dev (batch)** | `adk run` or pytest | Ollama | Multi-row sequential | Quick batch testing |
| **CI/test** | pytest + `Runner` | Mock / Ollama | Automated tests | No `adk web` needed |
| **Staging (API)** | `adk api_server` on Cloud Run | Vertex AI Gemini Flash | REST API (≤1K rows sync) | Auto-generated `/run` + `/run_sse` |
| **Staging (batch)** | `Runner` embedded in Dataflow | Vertex AI Gemini Flash | Distributed batch | Same agent code as API |
| **Production (API)** | `adk api_server` on Cloud Run | Vertex AI Gemini Flash | REST API (≤1K rows sync) | Production quota |
| **Production (batch)** | `Runner` embedded in Dataflow | Vertex AI Gemini Flash | Distributed batch (5M/day) | Autoscaling workers |

### 12.2 Cloud Run Deployment (API)

```dockerfile
FROM python:3.12-slim

# Install libpostal + dependencies
RUN apt-get update && apt-get install -y libpostal-dev
RUN pip install google-adk litellm

COPY src/ /app/src/
WORKDIR /app

# ADK api_server as the entrypoint
CMD ["adk", "api_server", "--host", "0.0.0.0", "--port", "8080"]
```

```yaml
# Cloud Run service
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: address-ai-api
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/minScale: "1"
        autoscaling.knative.dev/maxScale: "10"
    spec:
      containers:
        - image: gcr.io/project/address-ai:latest
          ports:
            - containerPort: 8080
          resources:
            limits:
              cpu: "2"
              memory: "1Gi"
          env:
            - name: GOOGLE_CLOUD_PROJECT
              value: "my-project"
            - name: ADK_MODEL
              value: "gemini-2.0-flash"
```

### 12.3 What We No Longer Build (ADK Provides It)

| v2 Component | v3 Status | Why |
|-------------|-----------|-----|
| Custom FastAPI API (`api/routes.py`) | **Replaced** by `adk api_server` | ADK generates `/run`, `/run_sse`, session management endpoints |
| Custom pipeline orchestrator (`pipeline/orchestrator.py`) | **Replaced** by `AddressPipelineAgent` | Agent handles orchestration + state management |
| Custom OpenTelemetry trace setup | **Simplified** — ADK generates events | Still use OTel for infra metrics, but agent traces are free |
| Custom dev debug tooling | **Replaced** by `adk web` | Browser UI with trace, state, tool call inspection |
| Custom evaluation harness | **Augmented** by ADK evaluation framework | Keep custom benchmark + add ADK eval |

---

## 13. Migration from v2 Design

### What Changes in v2 Documents

| v2 Section | Impact | Action |
|-----------|--------|--------|
| §4 Architecture Overview | Minor | Add note: "Pipeline orchestration uses ADK — see DESIGN_V3.md" |
| §6 Pipeline Steps | Unchanged | Step logic is identical; only the wiring changes |
| §9.6 Agentic Workflow | **Superseded** by this document | V3 expands Step 6 to the entire pipeline |
| §11 API Layer | **Simplified** | Replace custom FastAPI with `adk api_server` |
| §16 Deployment | Updated | Cloud Run runs `adk api_server` instead of custom FastAPI |
| All other sections | Unchanged | Infrastructure, data, security, cost model stay |

### Migration Steps

| Phase | Task | Effort |
|-------|------|--------|
| 1 | Install `google-adk`, set up project structure (`__init__.py`, `agent.py`) | 1 day |
| 2 | Implement 8 `CustomAgent` classes (wrap existing Python functions) | 2–3 days |
| 3 | Implement `LlmAddressParserAgent` (already designed in v2 §9.6) | 1 day |
| 4 | Implement `AddressPipelineAgent` orchestrator | 1 day |
| 5 | Test with `adk web` — verify trace visibility, state flow | 1 day |
| 6 | Wire into Dataflow (embed `Runner` in `ParDo`) | 1–2 days |
| 7 | Replace custom FastAPI with `adk api_server` on Cloud Run | 1 day |
| 8 | Set up ADK evaluation with benchmark dataset | 1 day |
| **Total** | | **~10 days** |

---

## 14. Benefits Summary

### Why Restructure the Pipeline as an ADK Agent Workflow?

| # | Benefit | Details |
|---|---------|---------|
| 1 | **Unified runtime** | `adk web` (dev) → `adk api_server` (API) → `Runner` (batch). Same agent code, three runtimes. No duplication. |
| 2 | **Free observability** | Every step emits ADK events. `adk web` renders interactive traces with tool call inspection, state diffs, latency breakdown — zero custom code. |
| 3 | **No custom API** | `adk api_server` auto-generates `/run`, `/run_sse`, session management. Replaces custom FastAPI. |
| 4 | **Built-in evaluation** | ADK's eval framework + our 500+ address benchmark. Run `adk eval` against the whole pipeline. |
| 5 | **State management** | `ctx.session.state` is the single source of truth. No custom dict-passing between steps. ADK manages sessions. |
| 6 | **Composable agents** | Need to add a "reviewer agent" later? Add another sub-agent. Need to parallelize Steps 2+3? Wrap them in a `ParallelAgent`. The architecture extends naturally. |
| 7 | **LLM abstraction** | `LlmAgent` model config: `"gemini-2.0-flash"` (prod) or `LiteLlm("ollama_chat/...")` (dev). One line to switch. |
| 8 | **Cloud deployment** | ADK deploys natively to Cloud Run, GKE, Agent Engine. Aligns with our GCP infrastructure. |
| 9 | **Multi-language option** | ADK has Python, TypeScript, Go, Java SDKs. Team can contribute in preferred language. |
| 10 | **Future-proof** | If we add multi-agent QA, A2A protocol, or streaming — ADK supports all of these. No framework migration needed. |

### What We Give Up

| Concern | Assessment |
|---------|-----------|
| ADK dependency | Single framework dependency. Risk: young ecosystem (2025). Mitigation: all business logic is in plain Python functions (tools + step functions) — ADK is just the wiring. Could be unwired in ~3 days. |
| Custom FastAPI control | Lose fine-grained endpoint customization. Mitigation: ADK api_server covers 95% of our needs. Can add custom endpoints via FastAPI middleware if needed. |
| Dataflow integration complexity | ADK Runner inside Dataflow ParDo is a non-standard pattern. Mitigation: Runner is lightweight; just needs `InMemorySessionService`. No external service dependencies. |

---

## 15. Risks & Mitigations

| # | Risk | Probability | Impact | Mitigation |
|---|------|-------------|--------|------------|
| 1 | ADK Runner performance overhead in Dataflow | Low | Medium | Runner + InMemorySessionService add < 1ms per row. Profile in staging. |
| 2 | ADK breaking changes (young framework) | Medium | Medium | Pin ADK version. Business logic is in plain Python functions — portable. |
| 3 | `adk api_server` lacks custom endpoint we need | Low | Low | Add custom middleware. Or run ADK alongside FastAPI on same service. |
| 4 | LiteLLM/Ollama tool calling incompatibility | Low | Medium | Test with Ollama models that support tool calling (qwen2.5-coder, gemma3). Fall back to prompt-based tool calling. |
| 5 | Session state grows too large for complex rows | Low | Low | State is per-row, ~2KB max. InMemorySessionService handles this trivially. |
| 6 | Team unfamiliarity with ADK | Medium | Medium | ADK API is simple (BaseAgent, LlmAgent, Runner). `adk web` is self-documenting. 1-day onboarding. |

---

## 16. Decision Record

### ADR-001: Entire Pipeline as ADK Agent Workflow

**Date:** 18 February 2026
**Status:** Proposed

**Context:**
DESIGN_V2.md designed the pipeline as custom Python with ADK only for Step 6 (LLM agent). ADK documentation reveals that `CustomAgent` (extends `BaseAgent`) can implement deterministic steps without an LLM, and `WorkflowAgent`s provide deterministic orchestration. This means the entire pipeline can run within ADK's agent framework.

**Decision:**
Restructure the 9-step pipeline as an ADK `CustomAgent` orchestrator (`AddressPipelineAgent`) with 9 sub-agents: 8 deterministic `CustomAgent`s + 1 `LlmAgent`.

**Consequences:**
- (+) Three free runtimes: `adk web`, `adk api_server`, `adk run`
- (+) Full-pipeline observability with zero custom code
- (+) Eliminates custom FastAPI and pipeline orchestrator
- (+) Built-in evaluation framework
- (+) Composable — easy to add agents, parallelize steps, or add multi-agent patterns
- (–) Adds ADK as a framework dependency
- (–) Dataflow integration requires embedding Runner in ParDo (non-standard but lightweight)
- (–) Team needs to learn ADK abstractions (BaseAgent, InvocationContext, session.state)

**Alternatives Considered:**
1. **Custom Python orchestrator (v2 design)** — more control, fewer dependencies, but requires building API, tracing, and evaluation from scratch
2. **LangGraph** — mature but heavy dependency tree, overkill for a sequential pipeline with one LLM step
3. **ADK only for Step 6 (v2 §9.6)** — misses the runtime, observability, and evaluation benefits for the deterministic steps

---

## Appendix A: ADK Documentation References

| Topic | URL |
|-------|-----|
| Agents overview | https://google.github.io/adk-docs/agents/ |
| LLM Agents | https://google.github.io/adk-docs/agents/llm-agents/ |
| Workflow Agents | https://google.github.io/adk-docs/agents/workflow-agents/ |
| Custom Agents | https://google.github.io/adk-docs/agents/custom-agents/ |
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

## Appendix B: File Structure (Proposed)

```
src/
├── address_ai/                    # ADK app directory
│   ├── __init__.py
│   ├── agent.py                   # AddressPipelineAgent + sub-agents + instantiation
│   ├── tools.py                   # GeoNames tool functions (query_city, etc.)
│   ├── prompts.py                 # LLM system prompt
│   ├── schemas.py                 # Pydantic models (LlmAddressOutput, etc.)
│   │
│   ├── agents/                    # Sub-agent implementations
│   │   ├── __init__.py
│   │   ├── preprocess.py          # PreprocessAgent
│   │   ├── libpostal_parse.py     # LibpostalParseAgent
│   │   ├── postal_lookup.py       # PostalCodeLookupAgent
│   │   ├── exact_match.py         # GeoNamesExactAgent
│   │   ├── mismatch_detect.py     # MismatchDetectAgent
│   │   ├── geonames_scan.py       # GeoNamesScanAgent
│   │   ├── llm_parser.py          # LlmAddressParserAgent (LlmAgent)
│   │   ├── revalidation.py        # RevalidationAgent
│   │   └── persist.py             # PersistAgent
│   │
│   ├── services/                  # Business logic (plain Python — no ADK dependency)
│   │   ├── __init__.py
│   │   ├── normalizer.py          # normalize_address()
│   │   ├── postal_extractor.py    # extract_postal_code()
│   │   ├── geonames_repo.py       # GeoNames DB queries
│   │   ├── disambiguation.py      # exact_match_with_disambiguation()
│   │   ├── mismatch_detector.py   # detect_country_mismatch()
│   │   ├── address_scanner.py     # scan_address_for_city()
│   │   └── persistence.py         # persist_to_cloud_sql(), write_to_gcs()
│   │
│   └── config.py                  # Settings (model, DB, Redis, etc.)
│
├── dataflow/                      # Dataflow batch integration
│   ├── pipeline.py                # Apache Beam pipeline with ProcessAddressFn
│   └── config.py                  # Dataflow job parameters
│
├── tests/
│   ├── test_agents/               # Agent-level tests
│   │   ├── test_preprocess.py
│   │   ├── test_exact_match.py
│   │   ├── test_orchestrator.py
│   │   └── ...
│   ├── test_services/             # Business logic tests (no ADK)
│   │   ├── test_normalizer.py
│   │   ├── test_disambiguation.py
│   │   └── ...
│   └── benchmark/
│       └── eval_dataset.json      # 500+ curated addresses
│
└── docs/
    ├── DESIGN.md                  # POC design (v1.2)
    ├── DESIGN_V2.md               # Production infrastructure
    └── DESIGN_V3.md               # This document — ADK pipeline architecture
```

**Key design principle:** All business logic lives in `services/` — **plain Python, no ADK dependency**. Agent classes in `agents/` are thin wrappers that call `services/` functions and read/write `session.state`. This means:
- Business logic is testable without ADK (`test_services/` → fast, no framework overhead)
- If we ever need to unwire ADK, we keep all the logic and only rewrite the thin agent wrappers
- Developers working on matching/disambiguation/scanning don't need to know ADK
