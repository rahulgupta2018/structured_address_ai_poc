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
| Scale | 13 rows, single file | Millions/day, streaming + batch |
| Data format | Excel in/out | Excel, CSV, JSON, API, message queue |
| GeoNames | In-memory dict (~400MB) | PostgreSQL + Redis cache |
| Disambiguation | Population tiebreak only | Postal code + admin1 + contextual signals |
| LLM | Local Ollama, 4 threads | Managed LLM service (Azure OpenAI / Bedrock), async with circuit breaker |
| Mismatch detection | None | Country-code vs. address content cross-validation |
| Deployment | CLI script | Containerized microservices (K8s) |
| Monitoring | Log files | OpenTelemetry + Prometheus + Grafana |
| Storage | File system | PostgreSQL + S3/Blob for audit |

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
| **No retry/dead-letter for LLM** | Row marked `needs_review` on LLM failure, no re-processing | Lost opportunities; human review queue grows unnecessarily |
| **Excel-only I/O** | `openpyxl` is slow for large files | Not viable for millions of rows |
| **No audit trail persistence** | Results only in output file | Cannot query historical results, track accuracy over time |

---

## 3. Production Requirements

### 3.1 Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-01 | Process ≥ 5 million addresses per day | P0 |
| FR-02 | Support input via API, CSV, Excel, and message queue | P0 |
| FR-03 | Achieve ≥ 90% auto-validation rate on well-formed input | P0 |
| FR-04 | Detect and flag country-code mismatches | P0 |
| FR-05 | Disambiguate same-name cities using postal code + admin hierarchy | P0 |
| FR-06 | Provide a human review UI for `needs_review` rows | P1 |
| FR-07 | Support incremental re-processing of failed/reviewed rows | P1 |
| FR-08 | Maintain full audit trail for every row (input → decision → output) | P0 |
| FR-09 | Support GeoNames dataset versioning and hot-reload | P1 |
| FR-10 | Support configurable LLM providers (Azure OpenAI, Bedrock, local Ollama) | P1 |

### 3.2 Non-Functional Requirements

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-01 | p95 latency — deterministic path | < 500ms |
| NFR-02 | p95 latency — LLM path | < 5s |
| NFR-03 | Availability | 99.9% (3-nines) |
| NFR-04 | Horizontal scalability | Auto-scale 2–20 workers based on queue depth |
| NFR-05 | Data retention | 7 years (regulatory compliance) |
| NFR-06 | PII handling | Encrypt at rest, redact in logs, GDPR-compliant |
| NFR-07 | Recovery time objective (RTO) | < 15 minutes |
| NFR-08 | Recovery point objective (RPO) | 0 (no data loss — persistent queue) |

---

## 4. Architecture Overview

### 4.1 High-Level Architecture

```
                    ┌──────────────────────────────────────┐
                    │          Ingestion Layer              │
                    │  (API Gateway / File Watcher / MQ)    │
                    └──────────────┬───────────────────────┘
                                   │
                    ┌──────────────▼───────────────────────┐
                    │         Message Queue (Kafka/SQS)     │
                    │   Topic: address.raw                  │
                    └──────────────┬───────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                     │
     ┌────────▼────────┐ ┌────────▼────────┐ ┌─────────▼───────┐
     │  Worker Pod 1   │ │  Worker Pod 2   │ │  Worker Pod N   │
     │                 │ │                 │ │                 │
     │ ┌─────────────┐ │ │ ┌─────────────┐ │ │ ┌─────────────┐ │
     │ │ Preprocessor │ │ │ │ Preprocessor │ │ │ │ Preprocessor │ │
     │ │ libpostal    │ │ │ │ libpostal    │ │ │ │ libpostal    │ │
     │ │ GeoNames     │ │ │ │ GeoNames     │ │ │ │ GeoNames     │ │
     │ │ Disambiguator│ │ │ │ Disambiguator│ │ │ │ Disambiguator│ │
     │ │ Decision Eng │ │ │ │ Decision Eng │ │ │ │ Decision Eng │ │
     │ └─────────────┘ │ │ └─────────────┘ │ │ └─────────────┘ │
     └────────┬────────┘ └────────┬────────┘ └─────────┬───────┘
              │                    │                     │
              └────────────────────┼─────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                     │
     ┌────────▼────────┐ ┌────────▼────────┐ ┌─────────▼───────┐
     │   Redis Cache   │ │   PostgreSQL    │ │  LLM Service    │
     │ (GeoNames +     │ │ (Audit trail +  │ │ (Azure OpenAI / │
     │  Postal codes)  │ │  Results + Jobs)│ │  Circuit Breaker)│
     └─────────────────┘ └─────────────────┘ └─────────────────┘
                                   │
                    ┌──────────────▼───────────────────────┐
                    │         Output Layer                  │
                    │  Topic: address.resolved              │
                    │  + API / S3 / Excel export            │
                    └──────────────────────────────────────┘
```

### 4.2 Component Responsibilities

| Component | Responsibility |
|-----------|---------------|
| **Ingestion Layer** | Accept input from REST API, file upload (CSV/Excel), or message queue. Validate schema, assign job ID, enqueue. |
| **Message Queue** | Decouple ingestion from processing. Enable backpressure, replay, and dead-letter routing. |
| **Worker Pods** | Stateless processing units running the full pipeline. Scale horizontally. Each pod has libpostal loaded. |
| **Redis Cache** | Shared GeoNames lookup cache + postal code mappings. Eliminates per-worker memory duplication. |
| **PostgreSQL** | Persistent storage for job metadata, row-level audit trail, disambiguation caches, and review queue. |
| **LLM Service** | Managed or self-hosted LLM with circuit breaker, rate limiting, and fallback providers. |
| **Output Layer** | Publish resolved addresses to downstream topic, S3 archive, or synchronous API response. |

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
Message Queue (address.raw)
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
  │     ├─ Same as POC but with disambiguation context
  │     ├─ Fuzzy match with pg_trgm (database-level, not in-process)
  │     ├─ match → ✅ validated (source=geonames_scan)
  │     └─ no match → LLM queue
  │
  ├─ Step 6: LLM Fallback (Enhanced)
  │     ├─ Async via managed service (Azure OpenAI / Bedrock)
  │     ├─ Circuit breaker pattern (fail-fast if service degraded)
  │     ├─ Enriched prompt with postal code context + mismatch warnings
  │     └─ Dead-letter queue for persistent failures
  │
  ├─ Step 7: Final Re-Validation (Enhanced)
  │     ├─ Exact + fuzzy re-validation (same as POC)
  │     ├─ Disambiguation applied to LLM result
  │     ├─ match → ✅ validated (source=llm)
  │     ├─ town present but no match → ⚠️ needs_review
  │     └─ no town → ❌ rejected
  │
  └─ Step 8: Persist & Publish
        ├─ Write to PostgreSQL (audit trail)
        ├─ Publish to address.resolved topic
        └─ Update job progress

```

### What Changed from POC

| Step | POC (v1.2) | Production (v2) | Why |
|------|------------|-----------------|-----|
| Step 0 | Basic normalization | + Postal code extraction from raw text | Feed disambiguation engine |
| Step 2 | — (did not exist) | Postal code cross-reference | Resolves ~80% of ambiguity |
| Step 3 | Population tiebreak | Multi-signal disambiguation | Eliminates silent wrong picks |
| Step 4 | — (did not exist) | Country-code mismatch detection | Catches 3/5 of POC's `needs_review` rows |
| Step 5 | In-process fuzzy scan | Database-backed pg_trgm | 10x faster, shared across workers |
| Step 6 | Local Ollama, sync | Managed LLM, async, circuit breaker | Scalability, reliability |
| Step 8 | Write Excel file | PostgreSQL + message queue + S3 | Audit, reprocessing, integration |

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
| **Azure OpenAI** (GPT-4o-mini) | Primary production provider | ~1–2s | ~$0.15/1M tokens |
| **AWS Bedrock** (Claude Haiku) | Secondary / fallback | ~1–2s | ~$0.25/1M tokens |
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
| **Deterministic-first** | Same as POC — only unresolved rows hit LLM |
| **Prompt caching** | Azure OpenAI prompt caching for identical system prompts |
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

---

## 10. Data Storage & Persistence

### 10.1 PostgreSQL Schema

```sql
-- ── Core Tables ──────────────────────────────────────────────

CREATE TABLE jobs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source          VARCHAR(50) NOT NULL,          -- 'api', 'file_upload', 'mq'
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

### 10.2 S3/Blob Storage

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
  Body: { file_url: "s3://...", format: "csv" }
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
  Response: { status, geonames_version, llm_status, queue_depth }
```

### 11.2 Message Queue Topics

| Topic | Direction | Schema |
|-------|-----------|--------|
| `address.raw` | Ingestion → Workers | `{ job_id, row_index, address_1, address_2, address_3, country_code }` |
| `address.resolved` | Workers → Output | `{ job_id, row_index, town, status, confidence, ... }` |
| `address.llm` | Workers → LLM Pool | `{ job_id, row_index, prompt, context }` |
| `address.dlq` | LLM Pool → Dead Letter | Failed LLM calls for retry/investigation |

---

## 12. Scalability & Performance

### 12.1 Throughput Targets

| Stage | POC Speed | v2 Target | How |
|-------|-----------|-----------|-----|
| Preprocessing + libpostal | ~1,000/s per process | ~10,000/s across cluster | 10 worker pods |
| GeoNames exact match | ~10,000/s (in-memory) | ~50,000/s (Redis cached) | Sub-ms Redis lookup |
| GeoNames fuzzy scan | ~100–500/s (in-process) | ~5,000/s (pg_trgm) | Database-level trigram index |
| LLM fallback | ~4–15/s (4 threads) | ~200–500/s (async, pooled) | Managed LLM with high concurrency |
| **End-to-end** | ~13 rows in 60s | **~5M rows / day** | Full pipeline |

### 12.2 Auto-Scaling Policy

```yaml
# Kubernetes HPA configuration
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: address-worker-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: address-worker
  minReplicas: 2
  maxReplicas: 20
  metrics:
    - type: External
      external:
        metric:
          name: kafka_consumer_lag
          selector:
            matchLabels:
              topic: address.raw
        target:
          type: AverageValue
          averageValue: "1000"    # Scale up when lag > 1000 messages per pod
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

### 12.3 Worker Pod Resource Profile

| Resource | Request | Limit | Notes |
|----------|---------|-------|-------|
| CPU | 1 core | 2 cores | libpostal is CPU-intensive |
| Memory | 512MB | 1GB | libpostal model (~200MB) + application |
| Disk | 500MB | 1GB | libpostal data files |

Note: GeoNames data is **not** loaded into worker memory (it's in PostgreSQL + Redis), so memory per worker is dramatically reduced from the POC's ~400MB overhead.

### 12.4 Database Sizing

| Component | Estimated Size | Notes |
|-----------|---------------|-------|
| `geonames_cities` | ~50MB | 166k records |
| `geonames_city_names` | ~500MB | ~5M name variants (with trigram index) |
| `geonames_postal_codes` | ~200MB | 2.5M records |
| `address_results` (30 days) | ~10GB | 5M rows/day × 30 days |
| `address_results` (1 year) | ~120GB | Partition by month |
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

---

## 13. Observability & Monitoring

### 13.1 Metrics (Prometheus)

| Metric | Type | Labels | Purpose |
|--------|------|--------|---------|
| `address_rows_processed_total` | Counter | `status` (validated/review/rejected) | Throughput |
| `address_processing_duration_seconds` | Histogram | `stage` (preprocess/match/llm/decision) | Latency per stage |
| `address_pipeline_duration_seconds` | Histogram | `source` (api/file/mq) | End-to-end latency |
| `address_geonames_cache_hit_ratio` | Gauge | `cache` (redis/db) | Cache effectiveness |
| `address_llm_calls_total` | Counter | `provider`, `status` (success/error/timeout) | LLM usage |
| `address_llm_tokens_total` | Counter | `provider`, `direction` (input/output) | LLM cost tracking |
| `address_llm_circuit_state` | Gauge | `provider` | 0=closed, 1=half-open, 2=open |
| `address_disambiguation_method_total` | Counter | `method` (postal/admin1/population) | Disambiguation signal usage |
| `address_mismatch_detected_total` | Counter | `action` (corrected/flagged) | Country-code issues |
| `address_review_queue_depth` | Gauge | — | Backlog for human review |
| `address_queue_lag` | Gauge | `topic` | Kafka consumer lag |

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
| **Infrastructure** | Worker pod count, CPU/memory utilization, Redis hit rate, PostgreSQL query latency, Kafka lag |
| **Review Queue** | Pending reviews, Avg review time, Corrections applied, Reviewer throughput |

---

## 14. Security & Compliance

### 14.1 Data Protection

| Control | Implementation |
|---------|---------------|
| **Encryption at rest** | PostgreSQL: TDE or AWS RDS encryption. S3: AES-256-SSE. Redis: encrypted cluster. |
| **Encryption in transit** | TLS 1.3 for all inter-service communication. mTLS between pods. |
| **PII handling** | Addresses are PII. Redact in all log messages (keep first 5 chars). |
| **Log sanitization** | `redact_pii()` applied to all structured logging. No raw addresses in logs. |
| **Access control** | RBAC via K8s service accounts. API authentication via OAuth2 / API keys. |
| **Data residency** | All processing within designated region. No cross-region data transfer. |
| **Retention policy** | Results: 7 years (regulatory). Logs: 90 days. Redis cache: 24h TTL. |

### 14.2 LLM-Specific Security

| Control | Implementation |
|---------|---------------|
| **No external LLM for sensitive data** | Azure OpenAI with data processing agreement. Or self-hosted Ollama in VPC. |
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
                    │  Tests   │  (Testcontainers: PG + Redis + Kafka)
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
│  │  - Kafka consumer                     │                       │
│  │  - Pipeline orchestrator              │                       │
│  │  - Connects to: Redis, PostgreSQL     │                       │
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
│  ├── PostgreSQL (RDS / CloudSQL)                                 │
│  ├── Redis Cluster (ElastiCache / Memorystore)                   │
│  ├── Kafka (MSK / Confluent)                                     │
│  ├── S3 / Blob Storage (audit archive)                           │
│  └── Azure OpenAI / AWS Bedrock (LLM)                            │
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

| Environment | Purpose | GeoNames | LLM Provider | Scale |
|-------------|---------|----------|--------------|-------|
| **Local** | Developer workstation | SQLite or in-memory | Ollama (local) | 1 worker |
| **CI** | Automated tests | Testcontainers PostgreSQL | Mock | 1 worker |
| **Staging** | Pre-production validation | PostgreSQL (small instance) | Azure OpenAI (dev tier) | 2 workers |
| **Production** | Live traffic | PostgreSQL (HA cluster) | Azure OpenAI (production) | 2–20 workers |

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

### Phase 3: API + Async Processing (Weeks 7–9)

| Task | Deliverable | Risk |
|------|-------------|------|
| Build FastAPI application | `api/` module with routes | Low |
| Set up Kafka topics + consumer | Message queue integration | Medium |
| Build job management (create, status, results) | `jobs/` module + DB tables | Medium |
| Build LLM provider abstraction | `llm/providers/` with Azure + Ollama | Low |
| Build circuit breaker | `llm/circuit_breaker.py` | Low |
| Add OpenTelemetry tracing | Distributed tracing across pipeline | Medium |

**Exit criterion:** API accepts batch submissions, processes asynchronously, returns results.

### Phase 4: Production Hardening (Weeks 10–12)

| Task | Deliverable | Risk |
|------|-------------|------|
| Kubernetes manifests + HPA | `k8s/` directory | Medium |
| CI/CD pipeline (GitHub Actions) | `.github/workflows/` | Low |
| Prometheus metrics + Grafana dashboards | Monitoring stack | Medium |
| Alerting rules | PagerDuty / Slack integration | Low |
| Security hardening (TLS, RBAC, PII redaction audit) | Security review pass | Medium |
| Load testing (5M rows) | Performance report | High |
| Chaos testing (LLM down, DB failover, Redis eviction) | Resilience report | High |
| Review workflow UI (basic) | Simple web form for `needs_review` rows | Medium |

**Exit criterion:** System handles 5M rows/day with < 1% error rate, auto-recovers from component failures.

---

## 18. Cost Estimation

### 18.1 Infrastructure (Monthly, AWS)

| Component | Spec | Monthly Cost |
|-----------|------|-------------|
| **EKS Cluster** | Control plane | ~$75 |
| **Worker Pods** (avg 5 × m5.large) | 2 vCPU, 8GB each | ~$350 |
| **API Pods** (2 × t3.medium) | 2 vCPU, 4GB each | ~$60 |
| **PostgreSQL** (RDS, db.r6g.large, Multi-AZ) | 2 vCPU, 16GB, 500GB SSD | ~$400 |
| **Redis** (ElastiCache, cache.m6g.large) | 2 vCPU, 6.4GB | ~$150 |
| **Kafka** (MSK, 3 brokers, kafka.m5.large) | — | ~$450 |
| **S3** (audit storage, ~500GB/year) | — | ~$12 |
| **NAT Gateway + Load Balancer** | — | ~$100 |
| **Total Infrastructure** | — | **~$1,600/month** |

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
| **GPT-4o-mini** | $0.15/1M | $0.60/1M | ~$45 | **~$1,350** |
| **Claude Haiku** | $0.25/1M | $1.25/1M | ~$84 | **~$2,520** |
| **Local Ollama** (GPU server) | — | — | ~$30 amortized | **~$900** |

With **response caching** (Redis, 30-day TTL), expect ~40% cache hit rate on recurring addresses, reducing LLM costs by ~40%.

### 18.3 Total Monthly Cost Estimate

| Component | Cost |
|-----------|------|
| Infrastructure | ~$1,600 |
| LLM (GPT-4o-mini, with caching) | ~$800 |
| **Total** | **~$2,400/month** |

---

## 19. Risk Register

| # | Risk | Probability | Impact | Mitigation |
|---|------|-------------|--------|------------|
| 1 | **LLM provider outage** | Medium | High — 15% of rows affected | Circuit breaker + dead-letter queue + fallback provider |
| 2 | **GeoNames data corruption** | Low | Critical — all lookups affected | Staging table + health check benchmark before swap |
| 3 | **Postal code data gaps** | Medium | Medium — disambiguation degrades | Graceful fallback to population tiebreak |
| 4 | **libpostal misparsing at scale** | Medium | Medium — more rows reach LLM | Monitor LLM fallback rate; retrain libpostal if available |
| 5 | **Input data quality worse than expected** | High | High — high needs_review rate | Mismatch detection + upstream data quality feedback loop |
| 6 | **Database performance under load** | Medium | High — latency spike | Connection pooling, read replicas, query optimization |
| 7 | **Redis eviction under memory pressure** | Low | Medium — cache miss spike → DB load | Monitor memory; size Redis appropriately |
| 8 | **Kafka consumer lag during peak** | Medium | Medium — processing delay | HPA auto-scaling; alert at 10k lag |
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
