# Structured Address AI v2 — Production Design Document (ISO 20022)

> **Version:** 2.0 — _February 2026_
> **Status:** Proposed — Production architecture building on POC v1.2
> **Audience:** Engineering, Architecture Review, Compliance

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [POC Retrospective — What Worked, What Didn't](#2-poc-retrospective--what-worked-what-didnt)
3. [Production Requirements](#3-production-requirements)
4. [Architecture Overview](#4-architecture-overview)
5. [Data Architecture](#5-data-architecture)
6. [Processing Pipeline v2 — Step by Step](#6-processing-pipeline-v2--step-by-step)
7. [Disambiguation Engine (New)](#7-disambiguation-engine-new)
8. [Country-Code Mismatch Detection (New)](#8-country-code-mismatch-detection-new)
9. [LLM Strategy for Production](#9-llm-strategy-for-production)
    - 9.6 [Agentic Workflow for Hard Rows (Step 6)](#96-agentic-workflow-for-hard-rows-step-6)
10. [Data Storage & Persistence](#10-data-storage--persistence)
11. [API Layer](#11-api-layer)
12. [Scalability & Performance](#12-scalability--performance)
13. [Observability & Monitoring](#13-observability--monitoring)
14. [Security & Compliance](#14-security--compliance)
15. [Testing Strategy](#15-testing-strategy)
16. [Deployment Architecture](#16-deployment-architecture)
17. [Migration Path — POC to Production](#17-migration-path--poc-to-production)
18. [Cost Estimation](#18-cost-estimation)
19. [Risk Register](#19-risk-register)
20. [Implementation Roadmap](#20-implementation-roadmap)

---

## 1. Executive Summary

### Background

The POC (v1.2) demonstrated a viable **deterministic-first, LLM-last** pipeline for converting unstructured multilingual addresses into ISO 20022-compliant structured output. On a 13-row test set it achieved 62% auto-validation, 38% needs_review, and 0% rejected — with zero false positives.

### Production Goal

Scale from **13 rows** to **millions of rows per day** while:

- Maintaining the zero-false-positive invariant (no row is `validated` without GeoNames confirmation)
- Reducing `needs_review` rate from 38% to < 10% through disambiguation and country-mismatch detection
- Achieving < 500ms p95 latency for deterministic paths and < 5s for LLM paths
- Supporting horizontal scaling, fault tolerance, and audit compliance

### Key Architectural Changes from POC

| Aspect | POC (v1.2) | Production (v2) |
|--------|------------|-----------------|
| Scale | 13 rows, single file | Millions/day, batch processing |
| Data format | Excel in/out | CSV batches (primary), Excel, API |
| GeoNames | In-memory dict (~400MB) | Cloud SQL (PostgreSQL) + Memorystore (Redis) cache |
| Disambiguation | Population tiebreak only | Postal code + admin1 + contextual signals |
| LLM | Local Ollama, 4 threads | Vertex AI, async concurrent (50/worker), circuit breaker, rate limiter |
| Mismatch detection | None | Country-code vs. address content cross-validation |
| Deployment | CLI script | GCP Dataflow (batch) + Cloud Run (API) + GKE (workers) |
| Monitoring | Log files | Cloud Monitoring + OpenTelemetry + Grafana |
| Storage | File system | Cloud SQL + GCS for audit |

---

## 2. POC Retrospective — What Worked, What Didn't

### ✅ What Worked Well

| Component | Evidence |
|-----------|----------|
| **Deterministic-first waterfall** | 6/13 rows resolved without any LLM call (Steps 1–3) |
| **GeoNames as ground truth** | Zero false positives — every `validated` row was correct |
| **libpostal parsing** | Correctly extracted town from well-formatted Western addresses |
| **LLM with post-validation** | Caught 2 additional rows (Zell am Ziller, Hluhluwe) via LLM → GeoNames re-validation |
| **Fuzzy re-validation** | Successfully matched partial/abbreviated LLM outputs to official GeoNames names |
| **Conservative defaults** | All uncertain rows correctly routed to `needs_review` |

### ⚠️ What Didn't Work / Gaps Identified

| Issue | Root Cause | Impact at Scale |
|-------|------------|-----------------|
| **3/13 rows had wrong `country_code`** | Upstream data quality (Barisardo in IE, Antwerpen in LT, Budapest in ES) | At millions of rows, ~5–15% may have wrong country codes → mass `needs_review` |
| **No disambiguation** | Population-based tiebreak only; no postal code or admin1 matching | ~1% silent wrong picks (e.g., wrong Springfield) → compliance risk |
| **Sequential file processing** | Single-threaded row loop; LLM concurrency within batch only | Cannot process millions of rows in acceptable time |
| **In-memory GeoNames** | 400MB RAM for cities1000.txt | Cannot scale to allCountries.txt filtered; no shared cache across workers |
| **No retry mechanism for LLM** | Row marked `needs_review` on LLM failure, no re-processing | Lost opportunities; human review queue grows unnecessarily |
| **Excel-only I/O** | `openpyxl` is slow for large files | Not viable for millions of rows |
| **No audit trail persistence** | Results only in output file | Cannot query historical results, track accuracy over time |

---

## 3. Production Requirements

### 3.1 Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-01 | Process ≥ 5 million addresses per day | P0 |
| FR-02 | Support input via CSV batch files (primary), API, and Excel | P0 |
| FR-03 | Achieve ≥ 90% auto-validation rate on well-formed input | P0 |
| FR-04 | Detect and flag country-code mismatches | P0 |
| FR-05 | Disambiguate same-name cities using postal code + admin hierarchy | P0 |
| FR-06 | Provide a human review UI for `needs_review` rows | P1 |
| FR-07 | Support incremental re-processing of failed/reviewed rows | P1 |
| FR-08 | Maintain full audit trail for every row (input → decision → output) | P0 |
| FR-09 | Support GeoNames dataset versioning and hot-reload | P1 |
| FR-10 | Support configurable LLM providers (Vertex AI, Azure OpenAI, local Ollama) | P1 |

### 3.2 Non-Functional Requirements

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-01 | p95 latency — deterministic path | < 500ms |
| NFR-02 | p95 latency — LLM path | < 5s |
| NFR-03 | Availability | 99.9% (3-nines) |
| NFR-04 | Horizontal scalability | Auto-scale Dataflow workers based on batch size; Cloud Run scales on request volume |
| NFR-05 | Data retention | 7 years (regulatory compliance) |
| NFR-06 | PII handling | Encrypt at rest, redact in logs, GDPR-compliant |
| NFR-07 | Recovery time objective (RTO) | < 15 minutes |
| NFR-08 | Recovery point objective (RPO) | ≤ 1,000 rows (≤ 1 chunk) — GCS staging + Cloud SQL checkpoints every 1k rows (see §12.6) |

---

## 4. Architecture Overview

### 4.1 High-Level Architecture

The production system uses two processing paths:
- **Batch path** (primary): CSV files processed via GCP Dataflow (Apache Beam) for high-volume daily runs
- **API path** (secondary): Cloud Run service for real-time / small-batch requests

```
                     ┌───────────────────────────────────────┐
                     │          Ingestion Layer               │
                     │  CSV Upload → GCS   /   REST API      │
                     └──────────┬──────────────┬─────────────┘
                                │              │
              ┌─────────────────▼──────┐  ┌────▼──────────────┐
              │   GCS Bucket           │  │  Cloud Run (API)  │
              │   gs://address-input/   │  │  FastAPI          │
              │   ├── batch_20260217/  │  │  (≤ 1000 rows     │
              │   │   └── chunk_*.csv  │  │   sync processing)│
              │   └── manifest.json    │  └────┬──────────────┘
              └─────────────┬──────────┘       │
                            │                  │
              ┌─────────────▼──────────┐       │
              │  GCP Dataflow Job       │       │
              │  (Apache Beam Pipeline) │       │
              │                         │       │
              │  ┌───────────────────┐  │       │
              │  │ ReadFromCSV       │  │       │
              │  │ Preprocess        │  │       │
              │  │ libpostal Parse   │  │       │
              │  │ PostalCodeLookup  │  │       │
              │  │ GeoNames Match    │  │       │
              │  │ Disambiguate      │  │       │
              │  │ MismatchDetect    │  │       │
              │  │ LLM Fallback      │  │       │
              │  │ Re-Validate       │  │       │
              │  │ Decision Engine   │  │       │
              │  │ WriteResults      │  │       │
              │  └───────────────────┘  │       │
              │                         │       │
              │  Auto-scales workers    │       │
              │  based on data volume   │       │
              └────────────┬────────────┘       │
                           │                    │
              ┌────────────┼────────────────────┼─────────┐
              │            │                    │         │
     ┌────────▼────────┐ ┌─▼────────────────┐ ┌─▼───────────────┐
     │  Memorystore    │ │  Cloud SQL       │ │  LLM Service    │
     │  (Redis)        │ │  (PostgreSQL)    │ │  (Vertex AI /   │
     │  GeoNames cache │ │  Audit trail +   │ │   Circuit       │
     │  + Postal codes │ │  Results + Jobs  │ │   Breaker)      │
     └─────────────────┘ └─────────────────┘  └─────────────────┘
                                   │
                    ┌──────────────▼───────────────────────┐
                    │         Output Layer                  │
                    │  GCS: gs://address-output/            │
                    │  ├── results_*.csv                    │
                    │  ├── needs_review_*.csv               │
                    │  └── audit_*.json                     │
                    │  + Cloud SQL (queryable results)      │
                    └──────────────────────────────────────┘
```

### 4.2 Component Responsibilities

| Component | Responsibility |
|-----------|---------------|
| **GCS (Input)** | Landing zone for CSV batch files. Supports manifest files for multi-file batches. Triggers Dataflow via Cloud Function or Cloud Scheduler. |
| **GCP Dataflow** | Distributed batch processing engine (Apache Beam). Auto-scales workers based on data volume. Handles parallelism, checkpointing, and fault tolerance natively. |
| **Cloud Run (API)** | Lightweight REST API for real-time / small-batch requests (≤ 1,000 rows). Runs the same pipeline logic synchronously. |
| **Memorystore (Redis)** | Shared GeoNames lookup cache + postal code mappings. Eliminates per-worker memory duplication. Accessible from Dataflow workers and Cloud Run. |
| **Cloud SQL (PostgreSQL)** | Persistent storage for job metadata, row-level audit trail, disambiguation caches, and review queue. |
| **LLM Service (via Google ADK)** | Agentic LLM orchestration via Google ADK. Vertex AI Gemini (prod), Ollama (dev) via LiteLLM wrapper. Agent has GeoNames tool access for self-correcting resolution. Circuit breaker, rate limiting, and fallback providers. |
| **GCS (Output)** | Resolved CSV files, needs_review extracts, and audit JSON logs. Downstream systems consume from output bucket. |

---

## 5. Data Architecture

### 5.1 GeoNames Data Layer

The POC loads `cities1000.txt` into a Python dict (~400MB). This does not scale to multiple workers or richer datasets.

#### Production Approach: PostgreSQL + Redis

```
                 GeoNames Raw Files
                 ├── cities1000.txt       (~166k records)
                 ├── postalCodes.zip       (~2.5M records)
                 └── admin1CodesASCII.txt  (~4k records)
                         │
                    ┌────▼────┐
                    │  ETL    │  (one-time + scheduled refresh)
                    │ Loader  │
                    └────┬────┘
                         │
                ┌────────▼────────┐
                │   PostgreSQL    │
                │                 │
                │  cities         │  geonameid, name, ascii, alts, cc, admin1, pop, lat, lon
                │  postal_codes   │  postal_code, cc, admin1, place_name, lat, lon
                │  admin1_codes   │  code, cc, name, ascii_name
                │  city_names     │  normalized_name, geonameid, cc, name_type (primary/ascii/alt)
                │                 │
                │  Indexes:       │
                │   - (cc, normalized_name)         -- exact lookup
                │   - (cc, admin1_code)             -- admin filtering
                │   - (cc, postal_code_prefix)      -- postal disambiguation
                │   - GIN trigram on normalized_name -- fuzzy search
                └────────┬────────┘
                         │
                    ┌────▼────┐
                    │  Redis  │  Hot cache (TTL: 24h)
                    │         │
                    │  Key patterns:
                    │   geo:exact:{cc}:{norm_name}  → [city records]
                    │   geo:names:{cc}              → set of all names
                    │   postal:{cc}:{prefix}        → [admin1, place]
                    │   admin1:{cc}:{code}          → region name
                    └─────────┘
```

#### Why PostgreSQL Instead of In-Memory Dict?

| Concern | In-Memory Dict (POC) | PostgreSQL + Redis (v2) |
|---------|----------------------|-------------------------|
| Memory per worker | ~400MB each | ~0 (shared DB) |
| 10 workers | 4GB total RAM just for GeoNames | ~0 per worker |
| Fuzzy search | O(n) scan per country | pg_trgm GIN index, sub-millisecond |
| Dataset updates | Restart all workers | Hot-reload via DB migration + cache invalidation |
| Postal code lookup | Not supported | Native table join |
| Admin1 hierarchy | Not supported | Native table join |
| Audit/traceability | geonames_id in output only | Full match history queryable |

#### GeoNames Refresh Strategy

```
┌──────────────────────────────────────────────────────────────┐
│  Weekly Cron Job                                              │
│                                                               │
│  1. Download latest cities1000.txt + postalCodes.zip          │
│  2. Load into staging tables (cities_staging, postal_staging) │
│  3. Diff against production tables                            │
│  4. Apply delta (new cities, updated names, removed entries)  │
│  5. Invalidate affected Redis keys                            │
│  6. Log version + record counts to audit table                │
│  7. Health check: verify known cities still resolve           │
│                                                               │
│  Rollback: swap staging ↔ production if health check fails    │
└──────────────────────────────────────────────────────────────┘
```

### 5.2 Postal Code Dataset

**Source:** GeoNames `postalCodes.zip` (~2.5M records worldwide)

| Column | Description |
|--------|-------------|
| `country_code` | ISO 3166-1 alpha-2 |
| `postal_code` | Full postal code |
| `place_name` | Associated city/town name |
| `admin_name1` | State/province name |
| `admin_code1` | State/province code (maps to `admin1CodesASCII.txt`) |
| `latitude` | Decimal degrees |
| `longitude` | Decimal degrees |

**Coverage:** ~80% of world countries have postal code data in GeoNames. Notable exceptions: some African and Asian countries with limited postal infrastructure.

**Index strategy:**

```sql
-- Exact postal code lookup (most common)
CREATE INDEX idx_postal_cc_code ON postal_codes (country_code, postal_code);

-- Prefix matching for countries with hierarchical codes (US, UK, DE)
-- US: first 3 digits = region; UK: first 2-4 chars = area; DE: first 2 = region
CREATE INDEX idx_postal_cc_prefix ON postal_codes (country_code, substr(postal_code, 1, 3));
```

### 5.3 Admin1 Hierarchy Dataset

**Source:** GeoNames `admin1CodesASCII.txt` (~4,000 records)

| Column | Description |
|--------|-------------|
| `code` | Composite key: `{country_code}.{admin1_code}` (e.g., `US.IL`, `DE.07`) |
| `name` | Full name in UTF-8 (e.g., `Illinois`, `Nordrhein-Westfalen`) |
| `ascii_name` | ASCII transliteration |
| `geonameid` | GeoNames ID of the admin1 region |

**Purpose:** Map the `admin1_code` field in `cities1000.txt` to human-readable region names, enabling:
- Cross-referencing libpostal's `state` label with GeoNames admin1
- Validating postal code → admin1 consistency
- Reporting matched region for traceability

---

## 6. Processing Pipeline v2 — Step by Step

### Pipeline Flow Diagram

```
CSV Batch (from GCS) or API Request
  │
  ├─ Step 0: Schema Validation & Preprocessing
  │     ├─ Validate input schema (Pydantic v2)
  │     ├─ Normalize text (NFKC, casefold, whitespace)
  │     └─ Extract postal code from raw text (regex per country format)
  │
  ├─ Step 1: libpostal Parse
  │     ├─ Extract town, street, building, postal_code, state
  │     └─ Build town_candidate
  │
  ├─ Step 2: Postal Code Cross-Reference (NEW)
  │     ├─ Lookup postal code → expected region + expected city
  │     ├─ If postal code maps to a known city → strong disambiguation signal
  │     └─ Store as context for downstream steps
  │
  ├─ Step 3: GeoNames Exact Validation (Enhanced)
  │     ├─ Exact match of town_candidate against GeoNames (country-scoped)
  │     ├─ If multiple matches → disambiguate using:
  │     │     1. Postal code region match
  │     │     2. Admin1 match (from libpostal state label)
  │     │     3. Population fallback (POC behaviour)
  │     ├─ match → ✅ validated (source=libpostal)
  │     └─ no match → unresolved
  │
  ├─ Step 4: Country-Code Mismatch Detection (NEW)
  │     ├─ Cross-validate country_code against address signals:
  │     │     - Postal code format vs country norms
  │     │     - Language detection on address text
  │     │     - City name found in different country's GeoNames
  │     ├─ If mismatch detected → add warning, attempt re-match with suggested country
  │     └─ Confidence adjustment for mismatch-corrected rows
  │
  ├─ Step 5: GeoNames Raw-Address Scan (Enhanced)
  │     ├─ Scan full raw address against country-filtered city lexicon
  │     │     (POC: in-memory n-gram exact + rapidfuzz token_set_ratio,
  │     │      longest match preferred, ambiguity margin filtering)
  │     ├─ v2 enhancement: disambiguation context passed to resolve ties
  │     ├─ Fuzzy match with pg_trgm (database-level, not in-process)
  │     ├─ match → ✅ validated (source=geonames_scan)
  │     └─ no match → LLM fallback
  │
  ├─ Step 6: Agentic LLM Fallback (NEW — replaces single-shot prompt)
  │     ├─ Unresolved rows dispatched to an LLM agent with GeoNames tool access
  │     ├─ Agent tools: query_city(), query_postal_code(), query_admin1(),
  │     │   search_city_fuzzy(), list_countries_for_city()
  │     ├─ Agent reasons over the address, calls tools to ground its answer,
  │     │   and self-corrects if initial parse doesn't match GeoNames
  │     ├─ Max 5 tool calls per row (budget cap to prevent runaway loops)
  │     ├─ Async concurrent agents (semaphore-bounded, ~50/worker)
  │     ├─ Circuit breaker + rate limiter (same infra as §9.2, §9.5)
  │     ├─ Per-agent timeout (15s) + retry with exponential backoff
  │     ├─ Agent output: structured JSON (town, street, building, postal_code)
  │     │   with tool_calls_log for full audit trail
  │     └─ Failed rows written to retry CSV for re-processing
  │
  ├─ Step 7: Final Re-Validation (Enhanced)
  │     ├─ Re-validate agent's town_candidate against GeoNames:
  │     │     1. Exact match (normalized name vs country-scoped lexicon)
  │     │     2. If no exact → fuzzy match (rapidfuzz token_set_ratio ≥ 80,
  │     │        ambiguity margin filtering, population tiebreak)
  │     ├─ Note: agent output is often pre-grounded (agent already
  │     │   queried GeoNames via tools), so re-validation is a safety net
  │     ├─ v2 enhancement: disambiguation signals applied to agent result
  │     ├─ match → ✅ validated (source=llm_agent)
  │     ├─ town present but no match → ⚠️ needs_review
  │     └─ no town → ❌ rejected
  │
  └─ Step 8: Persist & Output
        ├─ Write to Cloud SQL (audit trail)
        ├─ Write results CSV to GCS output bucket
        ├─ Write needs_review rows to separate CSV
        └─ Update job progress in Cloud SQL

```

### What Changed from POC

| Step | POC (v1.2) | Production (v2) | Why |
|------|------------|-----------------|-----|
| Step 0 | Basic normalization | + Postal code extraction from raw text | Feed disambiguation engine |
| Step 2 | — (did not exist) | Postal code cross-reference | Resolves ~80% of ambiguity |
| Step 3 | Population tiebreak | Multi-signal disambiguation | Eliminates silent wrong picks |
| Step 4 | — (did not exist) | Country-code mismatch detection | Catches 3/5 of POC's `needs_review` rows |
| Step 5 | In-process fuzzy scan | Database-backed pg_trgm | 10x faster, shared across workers |
| Step 6 | Local Ollama, single-shot prompt, 4 threads | **Agentic LLM via Google ADK** with GeoNames tool access (query_city, query_postal_code, query_admin1, search_city_fuzzy, list_countries_for_city). Agent reasons + self-corrects. Model-agnostic: Ollama (dev), Vertex AI Gemini (prod). Async semaphore (50 concurrent/worker). Circuit breaker + rate limiter. | Agent can resolve country-code mismatches, disambiguate via postal code, and handle messy multilingual input — all without hardcoded rules |
| Step 8 | Write Excel file | Cloud SQL + GCS output CSVs | Audit, reprocessing, integration |

---

## 7. Disambiguation Engine (New)

This is the **single most impactful addition** for production. It replaces the POC's population-based tiebreaking with a multi-signal scoring system.

### 7.1 Problem Statement

When the pipeline finds `"Springfield"` in a US address, `cities1000.txt` returns **30+ matches**. The POC picks the one with the highest population. In production, this causes:

- ~1% silent wrong picks (compliance risk)
- No traceability for why a specific city was chosen
- No confidence degradation for ambiguous matches

### 7.2 Disambiguation Signal Hierarchy

Signals are applied in order of reliability. Each signal either **resolves** the ambiguity or **narrows** the candidate set.

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Disambiguation Cascade                            │
│                                                                     │
│  Signal 1: Postal Code → Region                                    │
│  ┌──────────────────────────────────────────────┐                   │
│  │ postal_code "62701" → admin1 = "IL" (Illinois)│                  │
│  │ Confidence: VERY HIGH (postal codes are       │                  │
│  │ assigned by national post; rarely wrong)      │                  │
│  │ Coverage: ~80% of addresses have postal code  │                  │
│  └──────────────────────────────────────────────┘                   │
│       │                                                             │
│       ▼ If postal code resolves to single city → DONE               │
│       ▼ If postal code resolves to region → narrow candidates       │
│                                                                     │
│  Signal 2: Admin1 from Address (libpostal `state` label)            │
│  ┌──────────────────────────────────────────────┐                   │
│  │ libpostal extracts state = "IL"               │                  │
│  │ Map to admin1_code via admin1CodesASCII.txt   │                  │
│  │ Filter GeoNames candidates to matching admin1 │                  │
│  │ Confidence: HIGH (but libpostal state can be  │                  │
│  │ unreliable for some locales)                  │                  │
│  │ Coverage: ~60% of addresses have state/region │                  │
│  └──────────────────────────────────────────────┘                   │
│       │                                                             │
│       ▼ If single candidate remaining → DONE                       │
│       ▼ If still ambiguous → continue                               │
│                                                                     │
│  Signal 3: Postal Code ↔ Admin1 Cross-Validation                   │
│  ┌──────────────────────────────────────────────┐                   │
│  │ If both postal code and admin1 are available, │                  │
│  │ verify they agree. If they conflict → warning │                  │
│  │ (possible data quality issue)                 │                  │
│  │ If they agree → boost confidence              │                  │
│  └──────────────────────────────────────────────┘                   │
│       │                                                             │
│  Signal 4: Geographic Proximity                                     │
│  ┌──────────────────────────────────────────────┐                   │
│  │ If postal code provides lat/lon, prefer the   │                  │
│  │ GeoNames city closest to that coordinate.     │                  │
│  │ Useful when multiple cities share admin1.     │                  │
│  │ Confidence: MEDIUM                            │                  │
│  └──────────────────────────────────────────────┘                   │
│       │                                                             │
│  Signal 5: Population Fallback (POC behaviour)                      │
│  ┌──────────────────────────────────────────────┐                   │
│  │ If all signals exhausted, pick highest pop.   │                  │
│  │ Flag as `ambiguous_population_tiebreak`.      │                  │
│  │ Confidence: LOW — reduce score by 0.15        │                  │
│  └──────────────────────────────────────────────┘                   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 7.3 Disambiguation Confidence Adjustments

| Disambiguation Method | Confidence Modifier | Explanation |
|-----------------------|--------------------|-------------|
| Postal code → single city | No reduction (use base score) | Highest certainty |
| Postal code → region + single candidate in region | –0.02 | Very high certainty |
| Admin1 from address → single candidate | –0.05 | High certainty (libpostal state can be noisy) |
| Postal + admin1 agree → single candidate | –0.02 | Cross-validated, very reliable |
| Geographic proximity (< 10km) | –0.05 | Good but not definitive |
| Population tiebreak (no other signals) | –0.15 | Low certainty; flag for review if ambiguity > 3 |

### 7.4 Disambiguation Output Fields (New)

| Field | Type | Description |
|-------|------|-------------|
| `disambiguation_method` | `string \| null` | Which signal resolved the ambiguity |
| `disambiguation_candidates` | `int` | How many same-name cities existed in the country |
| `disambiguation_confidence` | `float` | Confidence in the disambiguation itself |
| `matched_admin1` | `string \| null` | Admin1 region of the matched city |
| `postal_code_region` | `string \| null` | Region derived from postal code lookup |

### 7.5 Postal Code Extraction from Raw Address

Many addresses contain a postal code embedded in the text even if libpostal doesn't cleanly extract it. Production v2 adds a **regex-based postal code extractor** with country-specific patterns:

| Country | Pattern | Example |
|---------|---------|---------|
| US | `\b\d{5}(-\d{4})?\b` | `62701`, `62701-1234` |
| UK | `\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b` | `SW1A 1AA` |
| DE, AT, CH | `\b\d{4,5}\b` | `80331`, `6280` |
| FR | `\b\d{5}\b` | `75001` |
| JP | `\b\d{3}-\d{4}\b` | `150-0001` |
| IN | `\b\d{6}\b` | `400001` |
| CA | `\b[A-Z]\d[A-Z]\s*\d[A-Z]\d\b` | `K1A 0B1` |
| NL | `\b\d{4}\s*[A-Z]{2}\b` | `1012 AB` |
| Generic | `\b\d{4,6}\b` (fallback) | Any 4–6 digit sequence |

The extractor runs after libpostal and supplements (does not replace) libpostal's `postcode` label. If both produce a postal code and they disagree, a warning is raised.

---

## 8. Country-Code Mismatch Detection (New)

### 8.1 Problem

In the POC, 3 of 5 `needs_review` rows had **wrong country codes** in the input data:

| Row | Address Content | Provided CC | Actual CC | City |
|-----|----------------|-------------|-----------|------|
| 8 | Italian address (Barisardo, Sardinia) | IE (Ireland) | IT (Italy) | Barisardo |
| 9 | Belgian address (Antwerpen) | LT (Lithuania) | BE (Belgium) | Antwerpen |
| 13 | Hungarian address (Budapest) | ES (Spain) | HU (Hungary) | Budapest |

These rows fail GeoNames validation because the city doesn't exist in the (wrong) country, and the LLM can't fix the country code.

### 8.2 Detection Signals

| Signal | Method | Reliability |
|--------|--------|-------------|
| **Cross-country city lookup** | If town_candidate not found in provided CC but found in exactly one other country → strong mismatch signal | HIGH — definitive for unique city names |
| **Postal code format** | Each country has characteristic postal code formats. `75001` is unmistakably French. `1012 AB` is unmistakably Dutch. | HIGH — format is country-specific |
| **Language detection** | Run fastText/langdetect on address text. `"Via Roma 15"` → Italian. `"Straße"` → German. | MEDIUM — short text, can be noisy |
| **Known landmark/pattern matching** | Patterns like `"PO Box"` (EN), `"Postfach"` (DE), `"BP"` (FR), `"Casella Postale"` (IT) | LOW — supplementary signal only |

### 8.3 Mismatch Resolution Strategy

```
If mismatch detected:
  │
  ├─ Case 1: City found in exactly ONE other country
  │     → Re-run validation with suggested_cc
  │     → If match: status = validated, confidence -= 0.10
  │     → Add warning: country_code_mismatch_corrected
  │     → Output both provided_cc and suggested_cc for audit
  │
  ├─ Case 2: City found in MULTIPLE other countries
  │     → Cannot auto-correct (which country is right?)
  │     → status = needs_review
  │     → review_reason = "country_code_ambiguous_mismatch"
  │     → List candidate countries in metadata
  │
  └─ Case 3: City not found anywhere
        → Continue normal pipeline (LLM fallback)
        → No mismatch signal available
```

### 8.4 Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ENABLE_MISMATCH_DETECTION` | `true` | Master toggle |
| `MISMATCH_AUTO_CORRECT` | `true` | Allow auto-correction for unambiguous mismatches |
| `MISMATCH_CONFIDENCE_PENALTY` | `0.10` | Confidence reduction for corrected rows |
| `MISMATCH_LANGUAGE_DETECTION` | `false` | Enable language-based detection (requires fastText) |

---

## 9. LLM Strategy for Production

### 9.1 Provider Abstraction

The POC is tightly coupled to Ollama. Production requires a provider-agnostic LLM layer:

```python
class LLMProvider(Protocol):
    """Abstract LLM provider interface."""

    async def complete(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 256,
        response_format: dict | None = None,
    ) -> LLMResponse: ...

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...
```

**Supported providers:**

| Provider | Use Case | Latency | Cost |
|----------|----------|---------|------|
| **Vertex AI** (Gemini 1.5 Flash) | Primary production provider | ~1–2s | ~$0.075/1M tokens |
| **Azure OpenAI** (GPT-4o-mini) | Secondary / fallback | ~1–2s | ~$0.15/1M tokens |
| **Local Ollama** | Dev/test, air-gapped deployments | ~3–5s | $0 (hardware cost only) |

### 9.2 Circuit Breaker Pattern

```
┌──────────────────────────────────────────────────┐
│              LLM Circuit Breaker                  │
│                                                   │
│  States:                                          │
│  ┌────────┐    5 failures    ┌────────┐           │
│  │ CLOSED │ ───────────────► │  OPEN  │           │
│  │(normal)│                  │(reject)│           │
│  └────┬───┘                  └───┬────┘           │
│       │                          │                │
│       │    success               │ 30s timeout    │
│       │◄─────────────────────┐   │                │
│       │                      │   ▼                │
│       │                  ┌───┴────────┐           │
│       │                  │ HALF-OPEN  │           │
│       │                  │(1 trial)   │           │
│       │                  └────────────┘           │
│                                                   │
│  When OPEN:                                       │
│  - Rows → needs_review with llm_circuit_open      │
│  - No HTTP calls made (fail-fast)                 │
│  - Alert fires to ops team                        │
│                                                   │
│  When HALF-OPEN:                                  │
│  - Send 1 probe request                           │
│  - Success → CLOSED (resume normal)               │
│  - Failure → OPEN (extend timeout)                │
└──────────────────────────────────────────────────┘
```

### 9.3 LLM Cost Control

At scale, LLM costs become significant. Controls:

| Control | Implementation |
|---------|---------------|
| **Deterministic-first** | Only unresolved rows hit LLM |
| **Prompt caching** | Vertex AI context caching for identical system prompts |
| **Response caching** | Cache LLM responses by `(normalized_address, country_code)` hash in Redis (TTL: 30d) |
| **Batch API** | Use OpenAI Batch API for non-real-time workloads (50% cost reduction) |
| **Token budget** | Per-hour token budget with automatic throttling |
| **Model tiering** | Use cheaper model (GPT-4o-mini) for simple cases, expensive model (GPT-4o) for retry-after-failure |

### 9.4 Enhanced Prompt (v2)

Production prompts include disambiguation context:

```json
{
  "address_1": "123 Main St",
  "address_2": "Springfield, IL 62701",
  "address_3": null,
  "country_code": "US",
  "parser_warnings": ["multiple_town_candidates"],
  "postal_code_context": {
    "extracted_postal_code": "62701",
    "postal_region": "Illinois",
    "postal_place": "Springfield"
  },
  "admin1_context": {
    "libpostal_state": "IL",
    "mapped_admin1": "Illinois"
  }
}
```

This gives the LLM **far more context** than the POC, reducing hallucination risk and improving accuracy for ambiguous cases.

### 9.5 LLM Concurrency Model

The LLM step is the **slowest stage** in the pipeline (~1–2s per call vs. < 5ms for deterministic steps). At 750,000 LLM rows/day, sequential processing would take ~208 hours. Concurrent async requests are essential.

#### Architecture: Async Semaphore-Bounded Concurrency

```
┌────────────────────────────────────────────────────────────────┐
│  Per Dataflow Worker / Cloud Run Instance                      │
│                                                                │
│  ┌──────────────────────────────────────────────────┐          │
│  │  asyncio Event Loop                               │          │
│  │                                                   │          │
│  │  Semaphore(LLM_CONCURRENCY=50)                    │          │
│  │  ┌─────┬─────┬─────┬─────┬─────┬──────────────┐   │          │
│  │  │ R1  │ R2  │ R3  │ ... │ R50 │  (waiting)   │   │          │
│  │  └──┬──┴──┬──┴──┬──┴─────┴──┬──┴──────────────┘   │          │
│  │     │     │     │           │                      │          │
│  │     ▼     ▼     ▼           ▼                      │          │
│  │  ┌──────────────────────────────────┐              │          │
│  │  │  aiohttp Session Pool             │              │          │
│  │  │  (connection pool size = 100)     │              │          │
│  │  │  TCP keep-alive, 30s timeout      │              │          │
│  │  └──────────────┬───────────────────┘              │          │
│  └─────────────────┼─────────────────────────────────┘          │
│                    │                                            │
│                    ▼                                            │
│            Vertex AI / Azure OpenAI                             │
│            (rate limit: ~1000 RPM per project)                  │
└────────────────────────────────────────────────────────────────┘
```

#### Implementation Pattern

```python
import asyncio
import aiohttp

class AsyncLLMPool:
    """Manages concurrent LLM requests with backpressure."""

    def __init__(
        self,
        provider: LLMProvider,
        max_concurrency: int = 50,       # concurrent in-flight requests
        request_timeout: float = 10.0,   # per-request timeout (seconds)
        rate_limit_rpm: int = 1000,      # requests per minute cap
    ):
        self._provider = provider
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._timeout = request_timeout
        self._rate_limiter = TokenBucketRateLimiter(rate_limit_rpm / 60)
        self._circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30)

    async def process_batch(self, rows: list[AddressRow]) -> list[LLMResult]:
        """Process a batch of rows with bounded concurrency."""
        tasks = [self._process_one(row) for row in rows]
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_one(self, row: AddressRow) -> LLMResult:
        async with self._semaphore:              # backpressure: max N in-flight
            await self._rate_limiter.acquire()    # respect provider rate limits
            if self._circuit_breaker.is_open:
                return LLMResult(status="skipped", reason="circuit_open")
            try:
                result = await asyncio.wait_for(
                    self._provider.complete(build_prompt(row)),
                    timeout=self._timeout,
                )
                self._circuit_breaker.record_success()
                return result
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                self._circuit_breaker.record_failure()
                return LLMResult(status="error", reason=str(e))
```

#### Concurrency Configuration

| Parameter | Default | Range | Notes |
|-----------|---------|-------|-------|
| `LLM_CONCURRENCY` | 50 | 10–200 | Concurrent in-flight requests per worker |
| `LLM_REQUEST_TIMEOUT` | 10s | 5–30s | Per-request timeout before abort |
| `LLM_RATE_LIMIT_RPM` | 1000 | — | Match provider's rate limit quota |
| `LLM_BATCH_SIZE` | 500 | 100–2000 | Rows collected before launching async batch |
| `LLM_RETRY_ATTEMPTS` | 2 | 0–3 | Retries per row (with exponential backoff) |

#### Throughput Math

```
Per worker:
  50 concurrent requests × ~1.5s avg latency = ~33 completions/sec
  = ~2,000 rows/min per worker

10 Dataflow workers:
  ~20,000 rows/min = ~1.2M rows/hour
  750,000 LLM rows/day → processed in ~38 minutes

Vs. sequential:
  1 request × 1.5s = 0.67 rows/sec
  750,000 rows → ~312 hours (13 days!) ❌
```

#### Backpressure & Fairness

- **Semaphore** prevents overwhelming the LLM provider. If 500 rows arrive but concurrency is 50, 450 wait.
- **Rate limiter** (token bucket) ensures we don't exceed the provider's RPM quota, even if requests return fast.
- **Circuit breaker** (§9.2) short-circuits all requests when the provider is degraded — no wasted timeouts.
- **Timeout** per request prevents a hung connection from blocking a semaphore slot forever.
- **Retry with exponential backoff**: 1st retry at 2s, 2nd at 4s. After max retries → `needs_review`.

#### Dataflow Integration

In Apache Beam, the LLM step is a `DoFn` with `setup()` creating the async pool:

```python
class LLMFallbackFn(beam.DoFn):
    def setup(self):
        """Called once per worker — create the async pool."""
        self._loop = asyncio.new_event_loop()
        self._pool = AsyncLLMPool(
            provider=VertexAIProvider(),
            max_concurrency=int(os.getenv("LLM_CONCURRENCY", "50")),
        )

    def process(self, batch: list[AddressRow]):
        results = self._loop.run_until_complete(
            self._pool.process_batch(batch)
        )
        for row, result in zip(batch, results):
            yield merge_llm_result(row, result)

    def teardown(self):
        self._loop.close()
```

Rows are **batched** before reaching this DoFn (via `beam.BatchElements(min_batch_size=100, max_batch_size=500)`) so the async pool processes chunks efficiently rather than one-at-a-time.

### 9.6 Agentic Workflow for Hard Rows (Step 6)

The POC uses a **single-shot LLM prompt**: send the address, get a JSON response, hope it's right. If it's wrong, there's no recovery — the row goes to `needs_review`.

Production v2 replaces this with an **agentic workflow**: the LLM gets access to GeoNames tools and can reason, query, and self-correct in a loop — like a human analyst would.

#### 9.6.1 Why Agentic?

| Approach | How it handles "Barisardo, 08042" with CC=IE |
|----------|-----------------------------------------------|
| **Single-shot prompt** (POC) | LLM outputs `{"town": "Barisardo"}`. Re-validation fails (not in IE). → `needs_review`. Dead end. |
| **Enriched prompt** (v2 §9.4) | We pre-compute postal context and feed it in the prompt. LLM might use it, might not. Still one shot. |
| **Agentic** (v2 §9.6) | Agent calls `query_city("barisardo", "IE")` → not found. Calls `list_countries_for_city("barisardo")` → `["IT"]`. Calls `query_postal_code("08042", "IT")` → Sardinia. Agent concludes: CC should be IT, town is Barisardo, Sardinia. Returns grounded answer + reasoning trace. |

The agentic approach handles **novel situations** we haven't explicitly coded rules for — which is exactly what the hard 15% of rows require.

#### 9.6.2 Agent Tools (GeoNames-Backed)

The agent has access to **5 read-only tools** that query the GeoNames database:

```python
@tool
def query_city(name: str, country_code: str) -> list[dict]:
    """Look up a city by name within a specific country.
    Returns: list of matches with geonameid, official_name, admin1, population.
    Returns empty list if no match found."""

@tool
def query_postal_code(postal_code: str, country_code: str) -> list[dict]:
    """Look up a postal code in a specific country.
    Returns: list of places with place_name, admin1_name, admin1_code, lat, lon.
    Useful for disambiguation and country-code verification."""

@tool
def query_admin1(country_code: str, admin1_code: str) -> dict | None:
    """Get the admin1 region name for a country + admin1 code.
    Example: query_admin1('US', 'IL') → {'name': 'Illinois', 'code': 'US.IL'}
    Useful for mapping state abbreviations to full names."""

@tool
def search_city_fuzzy(partial_name: str, country_code: str, limit: int = 5) -> list[dict]:
    """Fuzzy search for a city name within a country.
    Uses trigram similarity matching. Returns top matches sorted by similarity.
    Useful when the address contains misspellings or abbreviations."""

@tool
def list_countries_for_city(name: str) -> list[dict]:
    """Find ALL countries where a city name exists.
    Returns: list of {country_code, official_name, population} sorted by population.
    Useful for detecting country-code mismatches."""
```

**Constraints:**
- All tools are **read-only** — the agent cannot modify data
- Maximum **5 tool calls per row** — prevents runaway loops and controls cost
- Each tool call adds ~50 tokens to the context — budget is bounded
- Tools query the same Cloud SQL / Redis infrastructure used by deterministic steps

#### 9.6.3 Agent System Prompt

```
You are an address parsing specialist. Your task is to extract the correct
town/city name from an unstructured address.

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
```

#### 9.6.4 Agent Execution Flow

```
Input: {address_1: "Via Roma 15", address_2: "08042 Barisardo (OG)",
        address_3: "", country_code: "IE"}
  │
  ├─ Agent thought: "Address looks Italian (Via Roma), but CC is IE (Ireland).
  │                   Let me check if Barisardo exists in Ireland."
  │
  ├─ Tool call 1: query_city("barisardo", "IE")
  │   └─ Result: [] (not found in Ireland)
  │
  ├─ Agent thought: "Not in Ireland. Let me check other countries."
  │
  ├─ Tool call 2: list_countries_for_city("barisardo")
  │   └─ Result: [{country_code: "IT", name: "Barisardo", population: 3742}]
  │
  ├─ Agent thought: "Found only in Italy. Postal code 08042 — let me verify."
  │
  ├─ Tool call 3: query_postal_code("08042", "IT")
  │   └─ Result: [{place_name: "Bari Sardo", admin1: "Sardegna"}]
  │
  ├─ Agent thought: "Confirmed. Barisardo is in Sardinia, Italy.
  │                   Country code IE is wrong — should be IT."
  │
  └─ Output: {
       "town": "Barisardo",
       "street": "Via Roma 15",
       "building": null,
       "postal_code": "08042",
       "status": "validated",
       "suggested_country_code": "IT",
       "warnings": ["country_code_mismatch_corrected"],
       "tool_calls": 3,
       "reasoning": "City not found in IE. Unique match in IT confirmed by postal code."
     }
```

#### 9.6.5 Framework Evaluation

The agent needs: tool calling, structured output, async execution, observability. Here are the candidates:

| Framework | Pros | Cons | Fit |
|-----------|------|------|-----|
| **Google ADK** (Agent Development Kit) | **Model-agnostic**: Gemini (native), Ollama (via LiteLLM), Claude, any LiteLLM-supported model. Built-in tool calling, `FunctionTool`, workflow agents (Sequential, Parallel, Loop). Dev UI (`adk web`) with trace inspection. Built-in evaluation framework. Deploys to Cloud Run, GKE, Agent Engine. Python, TS, Go, Java SDKs. Google-maintained, active development. | Newer ecosystem (2025, now maturing). Smaller community than LangChain. | ⭐⭐⭐⭐⭐ |
| **LangGraph** (LangChain) | Mature. Large community. Graph-based state machine — explicit control over agent loops. Provider-agnostic. Great observability via LangSmith. Async native. | Heavy dependency tree (LangChain core + LangGraph, ~30+ transitive deps). Abstractions can be leaky. Over-engineered for a single-agent use case. Version churn — breaking changes between minor versions. | ⭐⭐⭐ |
| **OpenAI Agents SDK** | Simple, clean API. Native tool calling + structured output. Guardrails built in. Lightweight. | OpenAI-only (no Vertex AI, no Ollama). Vendor lock-in. Less mature for production. | ⭐⭐ (lock-in) |
| **Semantic Kernel** (Microsoft) | Enterprise-grade. Multi-provider. Good .NET + Python support. Planner + function calling. | Heavier abstraction. More complex setup. Microsoft-centric ecosystem. Overkill for a focused agent. | ⭐⭐ |
| **Vanilla Python + tool loop** | Zero dependencies. Full control. Easy to test and debug. Exact behaviour you code. Works with any LLM provider via REST. | You build everything yourself: tool dispatch, retry, context management, token budgeting. More code to maintain. No dev UI. | ⭐⭐⭐ |

#### 9.6.6 Detailed Framework Comparison

**Option A: Google ADK** ✅ CHOSEN

```python
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

# Production: Gemini via Vertex AI
agent = Agent(
    model="gemini-2.0-flash",
    name="address_parser",
    instruction=SYSTEM_PROMPT,
    tools=[query_city, query_postal_code, query_admin1,
           search_city_fuzzy, list_countries_for_city],
)

# Dev/local: Ollama via LiteLLM wrapper
agent_local = Agent(
    model=LiteLlm(model="ollama_chat/qwen2.5-coder:14b"),
    name="address_parser",
    instruction=SYSTEM_PROMPT,
    tools=[query_city, query_postal_code, query_admin1,
           search_city_fuzzy, list_countries_for_city],
)

# ADK handles: tool dispatch, context management, structured output
# Dev UI: `adk web` → trace every tool call in the browser
```

✅ Model-agnostic: Gemini (native), Ollama (via `LiteLlm`), Claude, any LiteLLM model
✅ Tool calling via plain Python functions — just add type hints + docstrings
✅ Built-in dev UI (`adk web`) with event trace, tool call inspection, latency visualization
✅ Built-in evaluation framework for systematic accuracy testing
✅ Workflow agents (Sequential, Parallel, Loop) available if we add multi-agent later
✅ Deploys to Cloud Run, GKE, or Agent Engine with minimal config
✅ Python, TypeScript, Go, Java SDKs — team can use preferred language
⚠️ Newer ecosystem (2025) but maturing fast with Google backing

**Option B: LangGraph**

```python
from langgraph.graph import StateGraph
from langchain_core.tools import tool
from langchain_google_vertexai import ChatVertexAI

# Define the graph: LLM node ↔ Tool node, with conditional edges
workflow = StateGraph(AgentState)
workflow.add_node("agent", call_model)
workflow.add_node("tools", call_tools)
workflow.add_conditional_edges(
    "agent",
    should_continue,  # if tool_calls → tools node, else → end
    {"continue": "tools", "end": END},
)
workflow.add_edge("tools", "agent")  # after tools → back to agent

agent = workflow.compile()
result = await agent.ainvoke({"messages": [format_address_prompt(row)]})
```

✅ Explicit graph — you can see and test every state transition
✅ Provider-agnostic (Vertex AI, OpenAI, Ollama all supported via LangChain)
✅ LangSmith for tracing/observability — see every tool call, every thought
✅ Mature, large community
⚠️ Pulls in LangChain core (~30+ transitive dependencies)
⚠️ Graph abstraction is powerful but overkill for a linear tool-calling loop
⚠️ Version churn — breaking changes between minor versions

**Option C: Vanilla Python (No Framework)**

```python
async def agent_parse_address(row: AddressRow, llm: LLMProvider, tools: ToolKit) -> AgentResult:
    """Minimal agentic loop — no framework dependency."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": format_address_prompt(row)},
    ]
    tool_calls_count = 0
    MAX_TOOL_CALLS = 5

    while tool_calls_count < MAX_TOOL_CALLS:
        response = await llm.complete(
            messages=messages,
            tools=tools.definitions,    # tool schemas for function calling
            temperature=0.0,
        )

        if response.tool_calls:
            for call in response.tool_calls:
                result = await tools.execute(call.name, call.arguments)
                messages.append({"role": "tool", "content": json.dumps(result), "tool_call_id": call.id})
                tool_calls_count += 1
        else:
            # Agent is done — parse final response
            return parse_structured_output(response.content)

    # Hit tool call budget — return what we have
    return AgentResult(status="needs_review", reason="tool_call_budget_exceeded")
```

✅ Zero framework dependencies — just your LLMProvider + tool functions
✅ Completely portable — works with any provider that supports tool calling
✅ Easy to unit test — mock the LLM, assert tool call sequence
✅ Full control over context window management and token budgeting
✅ ~50 lines of code — entire agent loop is auditable
⚠️ You build retry logic, observability, guardrails yourself
⚠️ No built-in tracing UI (but OpenTelemetry covers this)

#### 9.6.7 Decision: Google ADK

**Google ADK is the chosen framework for the agentic workflow.**

Rationale:
1. **Model-agnostic** — supports Gemini (native), Ollama (via `LiteLlm` wrapper), Claude (Anthropic), and any model supported by LiteLLM (100+ providers). No vendor lock-in.
2. **Minimal code** — tools are plain Python functions with type hints. No decorators, no schema boilerplate. ADK handles tool dispatch, context management, and response parsing.
3. **Built-in dev UI** — `adk web` gives browser-based agent interaction with event trace, tool call inspection, and latency visualization out of the box. No need for LangSmith or custom tracing.
4. **Built-in evaluation** — systematic accuracy testing against benchmark datasets, integrated into the framework.
5. **Workflow agents ready** — if we later add a reviewer agent or multi-agent QA pipeline, ADK's `SequentialAgent`, `ParallelAgent`, and `LoopAgent` are available without switching frameworks.
6. **GCP deployment native** — deploys to Cloud Run, GKE, or Agent Engine. Aligns with our GCP infrastructure (Dataflow, Cloud SQL, Memorystore).
7. **Multi-language** — Python (primary), TypeScript, Go, Java SDKs. Team flexibility.

#### Environment Model Matrix

| Environment | LLM Provider | ADK Model Config | Notes |
|-------------|-------------|-------------------|-------|
| **Local dev** | Ollama (qwen2.5-coder:14b) | `LiteLlm(model="ollama_chat/qwen2.5-coder:14b")` | Free, fast iteration, `adk web` for debugging |
| **CI/test** | Mock / Ollama | `LiteLlm(model="ollama_chat/gemma3:latest")` | Deterministic tests with mock; integration tests with Ollama |
| **Staging** | Vertex AI (Gemini Flash) | `"gemini-2.0-flash"` | Real LLM, dev-tier quota |
| **Production** | Vertex AI (Gemini Flash) | `"gemini-2.0-flash"` | Production quota, circuit breaker, rate limiter |
| **Fallback** | Claude via LiteLLM | `LiteLlm(model="anthropic/claude-sonnet-4-20250514")` | Secondary provider if Vertex AI degraded |

#### Why Not LangGraph?

LangGraph is mature and powerful, but:
- Pulls in LangChain core (~30+ transitive dependencies) — dependency weight we don't need
- Graph abstraction is overkill for a single-agent linear tool loop
- Version churn — breaking changes between minor versions have been a documented pain point
- ADK provides equivalent functionality (tools, tracing, evaluation) with a cleaner API

LangGraph remains a valid option if ADK's ecosystem proves insufficient, but as of Feb 2026, ADK covers all our requirements.

#### 9.6.8 Agent Observability & Audit

Every agent invocation produces an audit record:

```json
{
  "job_id": "abc-123",
  "row_index": 42,
  "agent_trace": {
    "tool_calls": [
      {"tool": "query_city", "args": {"name": "barisardo", "country_code": "IE"}, "result": [], "latency_ms": 3},
      {"tool": "list_countries_for_city", "args": {"name": "barisardo"}, "result": [{"country_code": "IT"}], "latency_ms": 5},
      {"tool": "query_postal_code", "args": {"postal_code": "08042", "country_code": "IT"}, "result": [{"place_name": "Bari Sardo"}], "latency_ms": 2}
    ],
    "total_tool_calls": 3,
    "reasoning": "City not found in IE. Unique match in IT confirmed by postal code.",
    "tokens_used": {"input": 580, "output": 120},
    "latency_ms": 2400
  },
  "output": {
    "town": "Barisardo",
    "suggested_country_code": "IT",
    "status": "validated"
  }
}
```

This is stored in the `address_results.metadata` JSONB column (§10.1) for full traceability.

#### 9.6.9 Cost Impact

The agentic approach uses more tokens per row (tool calls add context), but only applies to ~15% of rows:

| Metric | Single-shot (old) | Agentic (new) |
|--------|-------------------|---------------|
| Avg tool calls per row | 0 | ~2.5 |
| Avg input tokens per row | ~200 | ~450 (system + tools + tool results) |
| Avg output tokens per row | ~50 | ~120 (structured output + reasoning) |
| Cost per row (Gemini Flash) | ~$0.00003 | ~$0.00007 |
| Monthly cost (750K rows/day) | ~$336 | ~$756 |
| Accuracy improvement | Baseline | Est. +10–15% on hard rows (reduces needs_review) |

The ~$420/month increase is justified by significantly fewer `needs_review` rows reaching the human review queue (estimated 50% reduction in review volume → saves human reviewer time worth far more than $420/month).

---

## 10. Data Storage & Persistence

### 10.1 PostgreSQL Schema

```sql
-- ── Core Tables ──────────────────────────────────────────────

CREATE TABLE jobs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source          VARCHAR(50) NOT NULL,          -- 'api', 'file_upload', 'dataflow'
    input_file      TEXT,                           -- original filename (if file upload)
    total_rows      INT NOT NULL,
    processed_rows  INT DEFAULT 0,
    status          VARCHAR(20) DEFAULT 'pending',  -- pending, processing, completed, failed
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    metadata        JSONB                           -- geonames version, model used, etc.
);

CREATE TABLE address_results (
    result_id       BIGSERIAL PRIMARY KEY,
    job_id          UUID REFERENCES jobs(job_id),
    row_index       INT NOT NULL,

    -- Original input
    address_1       TEXT,
    address_2       TEXT,
    address_3       TEXT,
    country_code    CHAR(2) NOT NULL,

    -- Extracted output
    town            TEXT,
    street          TEXT,
    building        TEXT,
    postal_code     TEXT,

    -- Pipeline metadata
    status          VARCHAR(20) NOT NULL,           -- validated, needs_review, rejected
    confidence_score DECIMAL(4,3) NOT NULL,
    parser_source   VARCHAR(20),                    -- libpostal, geonames_scan, llm
    geonames_match  BOOLEAN DEFAULT FALSE,
    geonames_id     INT,
    normalized_town TEXT,
    warnings        JSONB DEFAULT '[]',
    review_reason   TEXT,

    -- Disambiguation metadata (v2)
    disambiguation_method   VARCHAR(50),
    disambiguation_candidates INT,
    matched_admin1  TEXT,
    postal_code_region TEXT,
    suggested_country_code CHAR(2),                 -- if mismatch detected

    -- Audit
    processing_time_ms INT,
    llm_tokens_used    INT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (job_id, row_index)
);

-- ── Indexes ──────────────────────────────────────────────

CREATE INDEX idx_results_job ON address_results (job_id);
CREATE INDEX idx_results_status ON address_results (status);
CREATE INDEX idx_results_review ON address_results (status) WHERE status = 'needs_review';
CREATE INDEX idx_results_country ON address_results (country_code);

-- ── Review Queue ─────────────────────────────────────────

CREATE TABLE review_queue (
    review_id       BIGSERIAL PRIMARY KEY,
    result_id       BIGINT REFERENCES address_results(result_id),
    assigned_to     TEXT,                           -- reviewer username
    review_status   VARCHAR(20) DEFAULT 'pending',  -- pending, in_review, approved, corrected, escalated
    corrected_town  TEXT,                           -- human-corrected value
    corrected_cc    CHAR(2),                        -- human-corrected country code
    reviewer_notes  TEXT,
    reviewed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── GeoNames Reference Tables ────────────────────────────

CREATE TABLE geonames_cities (
    geonameid       INT PRIMARY KEY,
    name            TEXT NOT NULL,
    ascii_name      TEXT,
    alternate_names TEXT[],
    country_code    CHAR(2) NOT NULL,
    admin1_code     TEXT,
    population      INT DEFAULT 0,
    latitude        DECIMAL(9,6),
    longitude       DECIMAL(9,6),
    feature_code    TEXT,
    loaded_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE geonames_city_names (
    id              BIGSERIAL PRIMARY KEY,
    geonameid       INT REFERENCES geonames_cities(geonameid),
    normalized_name TEXT NOT NULL,
    name_type       VARCHAR(10) NOT NULL,           -- primary, ascii, alternate
    country_code    CHAR(2) NOT NULL
);

CREATE INDEX idx_citynames_lookup ON geonames_city_names (country_code, normalized_name);
CREATE INDEX idx_citynames_trgm ON geonames_city_names USING gin (normalized_name gin_trgm_ops);

CREATE TABLE geonames_postal_codes (
    id              BIGSERIAL PRIMARY KEY,
    country_code    CHAR(2) NOT NULL,
    postal_code     TEXT NOT NULL,
    place_name      TEXT,
    admin_name1     TEXT,
    admin_code1     TEXT,
    latitude        DECIMAL(9,6),
    longitude       DECIMAL(9,6)
);

CREATE INDEX idx_postal_lookup ON geonames_postal_codes (country_code, postal_code);
CREATE INDEX idx_postal_prefix ON geonames_postal_codes (country_code, substr(postal_code, 1, 3));

CREATE TABLE geonames_admin1 (
    code            TEXT PRIMARY KEY,                -- e.g., "US.IL"
    name            TEXT NOT NULL,
    ascii_name      TEXT,
    geonameid       INT
);

CREATE TABLE geonames_versions (
    version_id      SERIAL PRIMARY KEY,
    dataset         TEXT NOT NULL,                   -- cities1000, postal_codes, admin1
    record_count    INT NOT NULL,
    loaded_at       TIMESTAMPTZ DEFAULT NOW(),
    file_hash       TEXT,                            -- SHA-256 of source file
    is_active       BOOLEAN DEFAULT TRUE
);
```

### 10.2 GCS (Cloud Storage)

| Bucket | Content | Retention |
|--------|---------|-----------|
| `address-ai-input/` | Original input files (Excel, CSV) | 7 years |
| `address-ai-output/` | Generated output files | 7 years |
| `address-ai-audit/` | Full pipeline trace per job (JSON) | 7 years |

---

## 11. API Layer

### 11.1 REST API

```
POST /api/v2/addresses/validate
  Body: { addresses: [{ address_1, address_2, address_3, country_code }] }
  Response: { job_id, results: [{ town, status, confidence, ... }] }
  Mode: Synchronous for ≤ 100 rows; async (returns job_id) for > 100

POST /api/v2/jobs
  Body: { file_url: "gs://address-input/...", format: "csv" }
  Response: { job_id, status: "pending", estimated_time_seconds: 120 }

GET /api/v2/jobs/{job_id}
  Response: { status, progress, total_rows, processed_rows, summary }

GET /api/v2/jobs/{job_id}/results
  Query: ?status=needs_review&page=1&limit=50
  Response: { results: [...], pagination: { total, page, pages } }

GET /api/v2/jobs/{job_id}/export
  Query: ?format=xlsx
  Response: Binary file download

POST /api/v2/review/{result_id}
  Body: { corrected_town, corrected_cc, reviewer_notes }
  Response: { review_id, status: "corrected" }

GET /api/v2/health
  Response: { status, geonames_version, llm_status, active_dataflow_jobs }
```

### 11.2 Batch Processing Model

Batch processing is the primary ingestion path. Files are uploaded to GCS and processed by GCP Dataflow.

| Stage | Component | Description |
|-------|-----------|-------------|
| **Upload** | GCS trigger / Cloud Scheduler | CSV files land in `gs://address-input/`. Cloud Function triggers Dataflow job or scheduled via Cloud Scheduler. |
| **Processing** | GCP Dataflow (Apache Beam) | Distributed pipeline: `ReadFromText` → `ParseCSV` → `ProcessAddress` (ParDo) → `WriteResults`. Auto-scales workers. |
| **Output** | GCS + Cloud SQL | Results CSV written to `gs://address-output/`. Row-level results persisted to Cloud SQL. |
| **Retry** | Retry CSV | Failed rows (LLM timeout, transient errors) written to a retry file for re-processing in next batch. |
| **Monitoring** | Cloud Monitoring | Job progress, worker count, and error rates tracked via Dataflow metrics. |

---

## 12. Scalability & Performance

### 12.1 Throughput Targets

| Stage | POC Speed | v2 Target | How |
|-------|-----------|-----------|-----|
| Preprocessing + libpostal | ~1,000/s per process | ~10,000/s across cluster | 10 Dataflow workers |
| GeoNames exact match | ~10,000/s (in-memory) | ~50,000/s (Redis cached) | Sub-ms Redis lookup |
| GeoNames fuzzy scan | ~100–500/s (in-process) | ~5,000/s (pg_trgm) | Database-level trigram index |
| LLM fallback | ~4–15/s (4 threads, sync) | ~330/s per worker (50 async concurrent × ~1.5s latency) → ~3,300/s across 10 workers | Async semaphore pool, aiohttp, rate-limited |
| **End-to-end** | ~13 rows in 60s | **~5M rows / day** | Full pipeline |

### 12.2 Auto-Scaling Policy

**GCP Dataflow (Batch Pipeline):**
Dataflow automatically manages worker scaling based on data volume:

```yaml
# Dataflow job launch parameters
worker_machine_type: n1-standard-2
num_workers: 2                 # Initial workers
max_num_workers: 20            # Max workers for autoscaling
autoscaling_algorithm: THROUGHPUT_BASED
disk_size_gb: 50
region: us-central1
network: address-ai-vpc
subnetwork: regions/us-central1/subnetworks/dataflow-subnet

# Autoscaling behavior:
# - Dataflow monitors per-step throughput and backlog
# - Scales up when backlog grows (more data than workers can handle)
# - Scales down when backlog shrinks
# - No manual HPA configuration needed
```

**Cloud Run (API):**
```yaml
# Cloud Run autoscaling
min_instances: 1
max_instances: 10
concurrency: 80
cpu: 2
memory: 1Gi
```

### 12.3 Dataflow Worker Resource Profile

| Resource | Request | Limit | Notes |
|----------|---------|-------|-------|
| CPU | 1 core | 2 cores | libpostal is CPU-intensive |
| Memory | 512MB | 1GB | libpostal model (~200MB) + application |
| Disk | 500MB | 1GB | libpostal data files |

Note: GeoNames data is **not** loaded into worker memory (it's in Cloud SQL + Memorystore Redis), so memory per worker is dramatically reduced from the POC's ~400MB overhead.

### 12.4 Database Sizing

**GeoNames reference tables (already loaded in dev SQLite — will migrate to Cloud SQL):**

| Component | Estimated Size | Notes |
|-----------|---------------|-------|
| `geonames_cities` | ~50MB | 166k records |
| `geonames_city_names` | ~500MB | ~5M name variants (with pg_trgm trigram index) |
| `geonames_postal_codes` | ~200MB | 1.8M records |
| `geonames_admin1` | < 1MB | 3.8k records |

**Production output tables (created when v2 pipeline goes live):**

| Component | Estimated Size | Notes |
|-----------|---------------|-------|
| `address_results` (30 days) | ~10GB | 5M rows/day × 30 days |
| `address_results` (1 year) | ~120GB | Partition by month (see §12.5) |
| `jobs` | < 1GB | Job metadata — low volume |
| `review_queue` | < 1GB | Subset of needs_review rows |
| Redis cache | ~2GB | Hot GeoNames data + LLM response cache |

### 12.5 PostgreSQL Partitioning

```sql
-- Partition address_results by month for manageability
CREATE TABLE address_results (
    ...
) PARTITION BY RANGE (created_at);

CREATE TABLE address_results_2026_02 PARTITION OF address_results
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');

-- Auto-create partitions via pg_partman or cron
```

### 12.6 Checkpointing & Crash Recovery

A 5M-row batch takes ~1–2 hours. Without checkpointing, a crash at row 4M loses all progress and requires a full re-run. This is unacceptable.

#### Strategy: Write-Ahead Partial Results

```
┌────────────────────────────────────────────────────────────────┐
│  Checkpointing Flow (per Dataflow job)                         │
│                                                                │
│  CSV Input (5M rows)                                           │
│    │                                                           │
│    ├─ Chunk 1 (rows 1–1,000)                                   │
│    │    ├─ Process all rows through pipeline                   │
│    │    ├─ Write results to Cloud SQL (batch INSERT)           │
│    │    ├─ Write partial CSV to GCS:                           │
│    │    │    gs://output/job_abc/chunk_00001.csv                │
│    │    ├─ Update job progress: processed_rows = 1,000         │
│    │    └─ ✅ Checkpoint committed                              │
│    │                                                           │
│    ├─ Chunk 2 (rows 1,001–2,000)                               │
│    │    ├─ Process → Write → Update progress                   │
│    │    └─ ✅ Checkpoint committed                              │
│    │                                                           │
│    ├─ ...                                                      │
│    │                                                           │
│    ├─ Chunk 4000 (rows 3,999,001–4,000,000)                    │
│    │    └─ ✅ Checkpoint committed                              │
│    │                                                           │
│    ├─ 💥 CRASH at row 4,000,042                                │
│    │                                                           │
│    │  Recovery:                                                │
│    │    1. Query Cloud SQL: last committed chunk = 4000         │
│    │    2. Resume from row 4,000,001                            │
│    │    3. Re-process only chunks 4001–5000                     │
│    │    4. Merge partial CSVs into final output                 │
│    │                                                           │
│    └─ Chunk 5000 (rows 4,999,001–5,000,000)                    │
│         └─ ✅ Job complete                                      │
│                                                                │
│  Final: merge gs://output/job_abc/chunk_*.csv                  │
│       → gs://output/job_abc/results_final.csv                   │
└────────────────────────────────────────────────────────────────┘
```

#### Checkpoint Granularity

| Parameter | Default | Notes |
|-----------|---------|-------|
| `CHECKPOINT_INTERVAL_ROWS` | 1,000 | Rows per checkpoint. At ~2KB/row, each commit is ~2MB — negligible overhead for Cloud SQL batch INSERT. |
| `CHECKPOINT_INTERVAL_SECONDS` | 60 (1 min) | Time-based fallback if row throughput is slow (e.g., many LLM calls). |
| `CHECKPOINT_TARGET` | `cloud_sql` | Where progress is recorded. Cloud SQL (primary) + GCS (partial CSVs). |

#### Implementation: Dataflow Native vs. Application-Level

**Dataflow's built-in checkpointing** handles worker-level fault tolerance automatically — if a worker dies, Dataflow reassigns its work to another worker. However, this is **within a single job run**. If the entire job fails (OOM, quota exceeded, network partition), Dataflow does NOT auto-resume from where it left off.

**Application-level checkpointing** (what we add) covers the job-level restart case:

```python
class CheckpointedPipeline:
    """Wraps the pipeline with chunk-based checkpointing."""

    def __init__(self, job_id: str, chunk_size: int = 1_000):
        self.job_id = job_id
        self.chunk_size = chunk_size

    def get_resume_offset(self) -> int:
        """Query Cloud SQL for last committed chunk."""
        row = db.execute(
            "SELECT processed_rows FROM jobs WHERE job_id = %s",
            (self.job_id,)
        ).fetchone()
        return row["processed_rows"] if row else 0

    def process_chunk(self, chunk: list[AddressRow], chunk_num: int):
        """Process one chunk and commit checkpoint."""
        results = [pipeline.process_row(row) for row in chunk]

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

#### Idempotency

Checkpointing requires idempotent writes — re-processing a chunk must produce the same result:

| Component | Idempotency Mechanism |
|-----------|----------------------|
| Cloud SQL results | `UNIQUE (job_id, row_index)` constraint + `ON CONFLICT DO UPDATE` (upsert) |
| GCS partial CSVs | Overwrite same chunk filename (GCS write is atomic per object) |
| Job progress | `UPDATE` is naturally idempotent |
| LLM calls | Deterministic (temperature=0) + response cache in Redis |

#### Crash Scenarios & Recovery

| Scenario | Data Loss | Recovery |
|----------|-----------|----------|
| Single Dataflow worker dies | 0 rows | Dataflow auto-retries the failed bundle on another worker |
| Entire Dataflow job fails | ≤ 1,000 rows (1 chunk) | Restart job with `--resume` flag; skips committed chunks |
| Cloud SQL connection lost mid-chunk | ≤ 1,000 rows | Transaction rolls back; chunk retried on next attempt |
| Redis cache lost | 0 rows | Cache miss → DB fallback. LLM responses re-fetched (no data loss, just slower) |
| GCS partial CSV write fails | 0 rows | Results already in Cloud SQL. CSV re-generated from DB at job completion. |

#### Monitoring Checkpoints

| Metric | Purpose |
|--------|---------|
| `address_checkpoint_committed_total` | Track checkpointing frequency |
| `address_checkpoint_duration_seconds` | Detect slow commits (DB bottleneck) |
| `address_job_resume_total` | How often jobs are resumed (crash frequency indicator) |
| `address_chunk_reprocess_total` | Rows re-processed after resume (waste indicator) |

---

## 13. Observability & Monitoring

### 13.1 Metrics (Prometheus)

| Metric | Type | Labels | Purpose |
|--------|------|--------|---------|
| `address_rows_processed_total` | Counter | `status` (validated/review/rejected) | Throughput |
| `address_processing_duration_seconds` | Histogram | `stage` (preprocess/match/llm/decision) | Latency per stage |
| `address_pipeline_duration_seconds` | Histogram | `source` (api/file/dataflow) | End-to-end latency |
| `address_geonames_cache_hit_ratio` | Gauge | `cache` (redis/db) | Cache effectiveness |
| `address_llm_calls_total` | Counter | `provider`, `status` (success/error/timeout) | LLM usage |
| `address_llm_tokens_total` | Counter | `provider`, `direction` (input/output) | LLM cost tracking |
| `address_llm_circuit_state` | Gauge | `provider` | 0=closed, 1=half-open, 2=open |
| `address_disambiguation_method_total` | Counter | `method` (postal/admin1/population) | Disambiguation signal usage |
| `address_mismatch_detected_total` | Counter | `action` (corrected/flagged) | Country-code issues |
| `address_review_queue_depth` | Gauge | — | Backlog for human review |
| `address_dataflow_workers_active` | Gauge | `job_id` | Dataflow worker count |

### 13.2 Distributed Tracing (OpenTelemetry)

Each address row gets a trace with spans for every pipeline stage:

```
Trace: job=abc123, row=42
  ├─ span: preprocess (2ms)
  ├─ span: libpostal_parse (5ms)
  ├─ span: postal_code_lookup (1ms, cache=hit)
  ├─ span: geonames_exact_match (3ms, cache=miss → db)
  │     └─ span: disambiguation (1ms, method=postal_code)
  ├─ span: decision_engine (< 1ms)
  └─ span: persist_result (2ms)
  Total: 14ms
```

### 13.3 Alerting Rules

| Alert | Condition | Severity | Action |
|-------|-----------|----------|--------|
| High needs_review rate | > 15% of rows in last 1h | Warning | Investigate input quality |
| LLM circuit open | Circuit breaker in OPEN state | Critical | Check LLM provider status |
| Queue lag > 10k | Consumer lag exceeds 10,000 | Warning | Scale up workers |
| Queue lag > 100k | Consumer lag exceeds 100,000 | Critical | Scale up + investigate bottleneck |
| GeoNames stale | No refresh in > 14 days | Warning | Check ETL job |
| Validation rate drop | < 80% (vs. 7-day average) | Warning | Investigate — new data pattern? |
| Error rate spike | > 1% of rows error in last 15m | Critical | Check logs, DB, Redis |
| Review queue backlog | > 5,000 pending reviews | Warning | Staff review team |

### 13.4 Dashboards (Grafana)

| Dashboard | Key Panels |
|-----------|------------|
| **Pipeline Overview** | Throughput (rows/min), Status distribution pie chart, p50/p95/p99 latency, Active jobs |
| **LLM Performance** | Calls/min, Avg latency, Error rate, Tokens consumed, Cost ($/hr), Circuit breaker state |
| **Data Quality** | Validation rate trend, Mismatch detection rate, Top review reasons, Disambiguation method distribution |
| **Infrastructure** | Dataflow worker count, CPU/memory utilization, Redis hit rate, Cloud SQL query latency, job throughput |
| **Review Queue** | Pending reviews, Avg review time, Corrections applied, Reviewer throughput |

---

## 14. Security & Compliance

### 14.1 Data Protection

| Control | Implementation |
|---------|---------------|
| **Encryption at rest** | Cloud SQL: CMEK encryption. GCS: AES-256 server-side. Memorystore Redis: encryption at rest. |
| **Encryption in transit** | TLS 1.3 for all inter-service communication. VPC-native Dataflow workers. |
| **PII handling** | Addresses are PII. Redact in all log messages (keep first 5 chars). |
| **Log sanitization** | `redact_pii()` applied to all structured logging. No raw addresses in logs. |
| **Access control** | RBAC via GCP IAM + Workload Identity. API authentication via OAuth2 / API keys. |
| **Data residency** | All processing within designated region. No cross-region data transfer. |
| **Retention policy** | Results: 7 years (regulatory). Logs: 90 days. Redis cache: 24h TTL. |

### 14.2 LLM-Specific Security

| Control | Implementation |
|---------|---------------|
| **No external LLM for sensitive data** | Vertex AI with data processing agreement. Or self-hosted Ollama in VPC. |
| **Prompt injection defence** | Input sanitization before prompt construction. System prompt is immutable. |
| **Output validation** | LLM output parsed as strict JSON schema. Arbitrary text rejected. |
| **Token budget limits** | Per-hour and per-day token caps to prevent runaway costs. |
| **Audit trail** | Every LLM call logged: input hash, output, tokens, latency, model version. |

### 14.3 SSRF Protection

| Layer | Control |
|-------|---------|
| **LLM endpoint** | Allowlist: only configured LLM provider URLs |
| **File upload** | Validate file extension + magic bytes. No URL-based file fetching. |
| **GeoNames refresh** | Hardcoded download URL (`download.geonames.org`). No user-configurable URLs. |

### 14.4 Compliance

| Regulation | Requirement | How We Comply |
|------------|-------------|---------------|
| **GDPR** | Right to erasure | Soft-delete with 30-day purge job. API endpoint for data deletion requests. |
| **SOC 2** | Audit trail | Every row has immutable audit record with timestamp, source, decision. |
| **ISO 20022** | Structured output format | Output schema validated against ISO 20022 address structure. |
| **Basel III/IV** | Data quality for financial instruments | Confidence scores + review workflow for uncertain results. |

---

## 15. Testing Strategy

### 15.1 Test Pyramid

```
                    ┌──────────┐
                    │  E2E     │  5% — Full pipeline via API
                    │  Tests   │  (Testcontainers: PG + Redis)
                    ├──────────┤
                    │Integration│ 20% — Multi-component tests
                    │  Tests   │  (DB queries, cache, LLM mock)
                    ├──────────┤
                    │  Unit    │  75% — Pure logic tests
                    │  Tests   │  (matching, scoring, disambiguation)
                    └──────────┘
```

### 15.2 Test Categories

| Category | Scope | Examples |
|----------|-------|---------|
| **Unit** | Individual functions | `test_disambiguate_postal_code()`, `test_mismatch_detection()`, `test_confidence_adjustment()` |
| **Integration** | Multi-component | `test_pipeline_with_postgres_geonames()`, `test_redis_cache_invalidation()`, `test_llm_circuit_breaker()` |
| **E2E** | Full API flow | `test_api_batch_submission()`, `test_file_upload_to_results()`, `test_review_workflow()` |
| **Performance** | Throughput + latency | `test_10k_rows_under_60s()`, `test_fuzzy_scan_p95_latency()` |
| **Chaos** | Failure scenarios | `test_llm_provider_down()`, `test_redis_unavailable()`, `test_db_failover()` |
| **Data quality** | Accuracy regression | `test_known_addresses_benchmark()` — 500+ curated addresses with expected results |

### 15.3 Benchmark Dataset

Build a curated benchmark of **500+ real-world addresses** covering:

| Category | Count | Purpose |
|----------|-------|---------|
| Well-formed Western addresses (US, UK, DE, FR) | 150 | Baseline accuracy |
| Non-Latin scripts (JP, CN, KR, AR, RU, TH) | 100 | Multilingual coverage |
| Same-name cities (Springfield, Richmond, etc.) | 50 | Disambiguation testing |
| Wrong country codes | 50 | Mismatch detection testing |
| Addresses with postal codes | 100 | Postal code disambiguation |
| Edge cases (PO Box, military, c/o, blank fields) | 50 | Robustness |

Each address has an **expected result**: `{ expected_town, expected_status, expected_country_code }`.

Run the benchmark after every release as a regression gate:
- `validated` accuracy must be ≥ 95% (correct town picked)
- `needs_review` rate must be ≤ 10% on well-formed input
- Zero false positives (wrong town marked `validated`)

---

## 16. Deployment Architecture

### 16.1 Kubernetes Topology

```
┌─────────────────────────────────────────────────────────────────┐
│  Kubernetes Cluster                                              │
│                                                                  │
│  Namespace: address-ai-prod                                      │
│                                                                  │
│  ┌──────────────────────────────────────┐                        │
│  │  Deployment: api-gateway (2 pods)     │                       │
│  │  - FastAPI application                │                       │
│  │  - Auth middleware (OAuth2)           │                       │
│  │  - Rate limiting                      │                       │
│  │  - Request validation                 │                       │
│  └──────────────────┬───────────────────┘                        │
│                     │                                            │
│  ┌──────────────────▼───────────────────┐                        │
│  │  Deployment: worker (2–20 pods, HPA)  │                       │
│  │  - libpostal loaded per pod           │                       │
│  │  - Pipeline orchestrator              │                       │
│  │  - Connects to: Redis, Cloud SQL      │                       │
│  └──────────────────┬───────────────────┘                        │
│                     │                                            │
│  ┌──────────────────▼───────────────────┐                        │
│  │  Deployment: llm-proxy (2 pods)       │                       │
│  │  - Provider abstraction layer         │                       │
│  │  - Circuit breaker                    │                       │
│  │  - Rate limiter                       │                       │
│  │  - Response caching                   │                       │
│  └──────────────────────────────────────┘                        │
│                                                                  │
│  ┌──────────────────────────────────────┐                        │
│  │  CronJob: geonames-refresh (weekly)   │                       │
│  │  - Download latest GeoNames files     │                       │
│  │  - ETL into PostgreSQL                │                       │
│  │  - Invalidate Redis cache             │                       │
│  │  - Run health check benchmark         │                       │
│  └──────────────────────────────────────┘                        │
│                                                                  │
│  ┌──────────────────────────────────────┐                        │
│  │  CronJob: review-report (daily)       │                       │
│  │  - Generate daily quality report      │                       │
│  │  - Track accuracy trends              │                       │
│  │  - Alert on anomalies                 │                       │
│  └──────────────────────────────────────┘                        │
│                                                                  │
│  External Services:                                              │
│  ├── Cloud SQL (PostgreSQL, HA)                                   │
│  ├── Memorystore (Redis Cluster)                                  │
│  ├── GCS (input/output/audit buckets)                             │
│  ├── GCP Dataflow (batch processing)                              │
│  └── Vertex AI / Azure OpenAI (LLM)                               │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 16.2 CI/CD Pipeline

```
Code Push → GitHub Actions
  │
  ├─ Lint + Type Check (ruff, mypy)
  ├─ Unit Tests (pytest, ~2 min)
  ├─ Integration Tests (Testcontainers, ~5 min)
  ├─ Build Docker Image
  ├─ Push to Container Registry
  │
  ├─ Deploy to Staging
  │     ├─ Run E2E tests against staging
  │     ├─ Run benchmark dataset (500+ addresses)
  │     ├─ Compare accuracy vs. last release
  │     └─ Gate: accuracy ≥ 95%, zero false positives
  │
  └─ Deploy to Production (manual approval)
        ├─ Rolling update (zero downtime)
        ├─ Canary: 10% traffic for 30 min
        ├─ Monitor error rate + latency
        └─ Full rollout or automatic rollback
```

### 16.3 Environment Matrix

| Environment | Purpose | GeoNames | LLM Provider | Processing |
|-------------|---------|----------|--------------|------------|
| **Local** | Developer workstation | SQLite or in-memory | Ollama (local) | Single-process Python |
| **CI** | Automated tests | Testcontainers PostgreSQL | Mock | Single-process Python |
| **Staging** | Pre-production validation | Cloud SQL (small instance) | Vertex AI (dev tier) | Dataflow (2 workers) |
| **Production** | Live batch + API | Cloud SQL (HA cluster) | Vertex AI (production) | Dataflow (2–20 workers) + Cloud Run |

---

## 17. Migration Path — POC to Production

### Phase 1: Foundation (Weeks 1–3)

| Task | Deliverable | Risk |
|------|-------------|------|
| Set up PostgreSQL + Redis infrastructure | Infrastructure-as-code (Terraform) | Low |
| Build GeoNames ETL (cities + postal codes + admin1) | `geonames_etl.py` + migration scripts | Low |
| Abstract GeoNames access behind interface | `GeoNamesRepository` (supports dict + DB) | Low |
| Port existing pipeline to use DB-backed GeoNames | All 49 existing tests still pass | Medium |

**Exit criterion:** Pipeline runs identically to POC but reads GeoNames from PostgreSQL.

### Phase 2: Disambiguation + Mismatch Detection (Weeks 4–6)

| Task | Deliverable | Risk |
|------|-------------|------|
| Build postal code extractor (regex per country) | `postal_extractor.py` | Low |
| Build postal code → region lookup | `PostalCodeRepository` | Low |
| Build disambiguation engine | `disambiguation.py` with signal cascade | Medium |
| Build country-code mismatch detector | `mismatch_detector.py` | Medium |
| Wire into pipeline as new steps | Pipeline v2 flow | Medium |
| Build benchmark dataset (500+ addresses) | `tests/benchmark/` | Medium |

**Exit criterion:** Benchmark accuracy ≥ 90%. Mismatch detection catches ≥ 80% of wrong country codes.

### Phase 3: API + Batch Processing (Weeks 7–9)

| Task | Deliverable | Risk |
|------|-------------|------|
| Build FastAPI application (Cloud Run) | `api/` module with routes | Low |
| Build GCP Dataflow pipeline (Apache Beam) | `dataflow/` module with ParDo transforms | Medium |
| Build job management (create, status, results) | `jobs/` module + DB tables | Medium |
| Build LLM provider abstraction | `llm/providers/` with Vertex AI + Ollama | Low |
| Build circuit breaker | `llm/circuit_breaker.py` | Low |
| Add OpenTelemetry tracing | Distributed tracing across pipeline | Medium |

**Exit criterion:** Dataflow pipeline processes CSV batches end-to-end. API accepts small-batch submissions.

### Phase 4: Production Hardening (Weeks 10–12)

| Task | Deliverable | Risk |
|------|-------------|------|
| Kubernetes manifests + HPA (for API/workers) | `k8s/` directory | Medium |
| Dataflow pipeline templates (for batch) | `dataflow/templates/` | Medium |
| CI/CD pipeline (GitHub Actions) | `.github/workflows/` | Low |
| Cloud Monitoring metrics + Grafana dashboards | Monitoring stack | Medium |
| Alerting rules | PagerDuty / Slack integration | Low |
| Security hardening (TLS, RBAC, PII redaction audit) | Security review pass | Medium |
| Load testing (5M rows) | Performance report | High |
| Chaos testing (LLM down, DB failover, Redis eviction) | Resilience report | High |
| Review workflow UI (basic) | Simple web form for `needs_review` rows | Medium |

**Exit criterion:** System handles 5M rows/day with < 1% error rate, auto-recovers from component failures.

---

## 18. Cost Estimation

### 18.1 Infrastructure (Monthly, GCP)

| Component | Spec | Monthly Cost |
|-----------|------|-------------|
| **GKE Cluster** | Autopilot (API + workers) | ~$75 |
| **Cloud Run** (API, avg 2 instances) | 2 vCPU, 1GB each | ~$60 |
| **GCP Dataflow** (avg 5 workers, n1-standard-2) | Batch jobs ~8hrs/day | ~$300 |
| **Cloud SQL** (PostgreSQL, db-n1-standard-2, HA) | 2 vCPU, 7.5GB, 500GB SSD | ~$350 |
| **Memorystore Redis** (M1, 5GB) | Standard tier, HA | ~$150 |
| **GCS** (audit + input/output, ~500GB/year) | Standard storage | ~$12 |
| **Cloud NAT + Load Balancer** | — | ~$80 |
| **Total Infrastructure** | — | **~$1,027/month** |

### 18.2 LLM Costs (Monthly)

Assumes 5M rows/day, ~15% reach LLM fallback (after disambiguation improvements):

| Parameter | Value |
|-----------|-------|
| LLM rows per day | 750,000 |
| Avg input tokens per row | ~200 |
| Avg output tokens per row | ~50 |
| Input tokens per day | ~150M |
| Output tokens per day | ~37.5M |

| Provider | Input Cost | Output Cost | Daily | Monthly |
|----------|-----------|-------------|-------|---------|
| **Gemini 1.5 Flash** (Vertex AI) | $0.075/1M | $0.30/1M | ~$19 | **~$560** |
| **GPT-4o-mini** (fallback) | $0.15/1M | $0.60/1M | ~$45 | **~$1,350** |
| **Local Ollama** (GPU server) | — | — | ~$30 amortized | **~$900** |

With **response caching** (Memorystore Redis, 30-day TTL), expect ~40% cache hit rate on recurring addresses, reducing LLM costs by ~40%.

### 18.3 Total Monthly Cost Estimate

| Component | Cost |
|-----------|------|
| Infrastructure | ~$1,027 |
| LLM (Gemini Flash, with caching) | ~$336 |
| **Total** | **~$1,363/month** |

---

## 19. Risk Register

| # | Risk | Probability | Impact | Mitigation |
|---|------|-------------|--------|------------|
| 1 | **LLM provider outage** | Medium | High — 15% of rows affected | Circuit breaker + retry CSV + fallback provider |
| 2 | **GeoNames data corruption** | Low | Critical — all lookups affected | Staging table + health check benchmark before swap |
| 3 | **Postal code data gaps** | Medium | Medium — disambiguation degrades | Graceful fallback to population tiebreak |
| 4 | **libpostal misparsing at scale** | Medium | Medium — more rows reach LLM | Monitor LLM fallback rate; retrain libpostal if available |
| 5 | **Input data quality worse than expected** | High | High — high needs_review rate | Mismatch detection + upstream data quality feedback loop |
| 6 | **Database performance under load** | Medium | High — latency spike | Connection pooling, read replicas, query optimization |
| 7 | **Redis eviction under memory pressure** | Low | Medium — cache miss spike → DB load | Monitor memory; size Redis appropriately |
| 8 | **Dataflow worker starvation during peak** | Medium | Medium — processing delay | Dataflow autoscaling (max 20 workers); Cloud Monitoring alerts on backlog |
| 9 | **Cost overrun on LLM** | Medium | Medium — budget exceeded | Token budgets, response caching, Batch API for non-RT |
| 10 | **GDPR data deletion request** | Low | Medium — operational burden | Automated purge job, soft-delete architecture |

---

## 20. Implementation Roadmap

### Timeline Overview

```
Week  1  2  3  4  5  6  7  8  9  10 11 12 13 14
──────────────────────────────────────────────────
Phase 1: Foundation
████████████████
                  Phase 2: Disambiguation
                  ████████████████
                                    Phase 3: API + Async
                                    ████████████████
                                                      Phase 4: Hardening
                                                      ████████████████
```

### Milestones

| Milestone | Week | Deliverable | Success Criteria |
|-----------|------|-------------|------------------|
| **M1: DB Migration** | 3 | Pipeline runs on PostgreSQL + Redis | All 49 POC tests pass; identical results |
| **M2: Disambiguation** | 6 | Postal code + admin1 + mismatch detection | Benchmark accuracy ≥ 90%; POC's 3 mismatch rows detected |
| **M3: API MVP** | 9 | REST API + async job processing | Submit 10k rows via API; results returned < 5 min |
| **M4: Production Ready** | 12 | Full stack deployed to staging | 5M row load test passes; chaos tests pass |
| **M5: GA Release** | 14 | Production deployment | 1 week of stable production traffic |

### Team Sizing

| Role | Count | Focus |
|------|-------|-------|
| Backend Engineer | 2 | Pipeline, disambiguation, API |
| Data Engineer | 1 | GeoNames ETL, PostgreSQL, benchmark dataset |
| DevOps / SRE | 1 | K8s, CI/CD, monitoring, alerting |
| QA Engineer | 1 | Test strategy, benchmark curation, chaos testing |
| **Total** | **5** | **14 weeks to GA** |

---

## Appendix A: POC → v2 Component Mapping

| POC Module | v2 Equivalent | Changes |
|------------|--------------|---------|
| `config.py` | `config/settings.py` (Pydantic BaseSettings) | Environment-based config with validation; provider configs |
| `pipeline.py` | `pipeline/orchestrator.py` | Async, message-driven, new steps (postal, disambiguation, mismatch) |
| `geonames_loader.py` | `etl/geonames_etl.py` + `repositories/geonames_repo.py` | ETL loads to PostgreSQL; repo abstracts DB + Redis |
| `geonames_matcher.py` | `matching/exact_matcher.py` + `matching/fuzzy_matcher.py` | DB-backed, pg_trgm for fuzzy |
| `geonames_scan.py` | `matching/address_scanner.py` | DB-backed scan with disambiguation context |
| `llm_ollama.py` | `llm/providers/` + `llm/circuit_breaker.py` | Multi-provider, circuit breaker, response caching |
| `decision_engine.py` | `engine/decision_engine.py` | Enhanced with disambiguation + mismatch context |
| `io_excel.py` | `io/excel.py` + `io/csv.py` + `api/routes.py` | Multi-format I/O + REST API |
| `schemas.py` | `models/` directory | Extended with disambiguation + review + job models |
| `preprocess.py` | `preprocessing/normalizer.py` + `preprocessing/postal_extractor.py` | + Postal code regex extraction |
| — (new) | `disambiguation/engine.py` | Postal code + admin1 + proximity + population cascade |
| — (new) | `mismatch/detector.py` | Country-code mismatch detection + correction |
| — (new) | `review/queue.py` + `review/api.py` | Human review workflow |
| — (new) | `observability/metrics.py` + `observability/tracing.py` | Prometheus + OpenTelemetry |

---

## Appendix B: GeoNames Dataset Summary

| Dataset | File | Records | Size | Purpose in v2 |
|---------|------|---------|------|----------------|
| `cities1000.txt` | TSV | ~166,000 | ~25MB | Primary city gazetteer |
| `postalCodes.zip` | TSV | ~2,500,000 | ~60MB | Postal code → region/city mapping |
| `admin1CodesASCII.txt` | TSV | ~4,000 | ~200KB | State/province code → name mapping |
| `countryInfo.txt` | TSV | ~252 | ~30KB | Country metadata (optional, for format validation) |

All downloadable from `https://download.geonames.org/export/dump/`.

---

## Appendix C: Confidence Score Policy v2

### Updated Score Table

| Scenario | Base Score | Disambiguation Modifier | Final Range |
|----------|-----------|------------------------|-------------|
| libpostal → exact primary name, postal-code confirmed | 1.00 | 0.00 | **1.00** |
| libpostal → exact primary name, no disambiguation needed (unique name) | 1.00 | 0.00 | **1.00** |
| libpostal → exact primary name, population tiebreak (ambiguous) | 1.00 | –0.15 | **0.85** |
| libpostal → exact alternate name, postal-code confirmed | 0.95 | 0.00 | **0.95** |
| GeoNames scan → fuzzy match, postal-code confirmed | 0.80 | 0.00 | **0.80** |
| GeoNames scan → fuzzy match, population tiebreak | 0.80 | –0.15 | **0.65** |
| LLM → exact GeoNames match, postal-code confirmed | 0.75 | 0.00 | **0.75** |
| LLM → fuzzy GeoNames match | 0.70 | varies | **0.55–0.70** |
| Country-code mismatch corrected → validated | varies | –0.10 | **varies** |
| LLM → no GeoNames match | 0.40 | — | **0.40** |
| No candidate | 0.00 | — | **0.00** |

### New Hard Rule (v2)

> **If a row is disambiguated via population tiebreak only (no postal code, no admin1 signal), and there are ≥ 3 same-name cities in the country, the row is downgraded to `needs_review` regardless of match quality.**

This prevents silent wrong picks for highly ambiguous city names at scale.
