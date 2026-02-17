# Structured Address AI — Design Document (ISO 20022)

> **Version:** 1.2 — _February 2026_
> **Status:** Implemented — POC complete with all design decisions resolved.

---

## Table of Contents

1. [Objective](#1-objective)
2. [Approach Overview](#2-approach-overview)
3. [Design Rationale](#3-design-rationale)
4. [Tech Stack](#4-tech-stack)
5. [Data Sources & Coverage](#5-data-sources--coverage)
6. [Input / Output Contracts](#6-input--output-contracts)
7. [Processing Pipeline — Step by Step](#7-processing-pipeline--step-by-step)
8. [Anti-Hallucination & Bias Controls](#8-anti-hallucination--bias-controls)
9. [Confidence Score Policy](#9-confidence-score-policy)
10. [Error Handling & Resilience](#10-error-handling--resilience)
11. [Performance & Scalability](#11-performance--scalability)
12. [Implementation Plan](#12-implementation-plan)
13. [Project Structure](#13-project-structure)
14. [Design Decisions](#14-design-decisions)
15. [Immediate Next Tasks](#15-immediate-next-tasks)

---

## 1. Objective

Build a **deterministic-first pipeline** to convert unstructured, multilingual addresses into ISO 20022-compliant structured output. The primary extraction target is `town`, with secondary extraction of `street`, `building`, and `postal_code`.

### Input Fields

| Field          | Type     | Required |
|----------------|----------|----------|
| `address_1`    | string   | nullable |
| `address_2`    | string   | nullable |
| `address_3`    | string   | nullable |
| `country_code` | string   | **required** (ISO 3166-1 alpha-2) |

### Primary Output

| Field              | Description                                      |
|--------------------|--------------------------------------------------|
| `town`             | Extracted and validated town/city name            |
| `status`           | `validated` · `needs_review` · `rejected`        |
| `confidence_score` | 0.0–1.0 composite score                          |
| `parser_source`    | Which stage resolved the town                    |
| `warnings`         | Structured list of issues encountered            |

### Design Principles

1. **`libpostal`** for initial parsing — a rule/model-based multilingual parser, no custom training required.
2. **`cities1000.txt`** (GeoNames gazetteer) for strict geographic grounding — the system never trusts a town name without external validation.
3. **LLM fallback only** for rows that deterministic logic cannot resolve — minimizing cost, latency, and hallucination risk.

---

## 2. Approach Overview

The pipeline is a **waterfall with escalation**: each stage resolves what it can and only passes unresolved rows to the next, more expensive stage.

```
Excel Input
  │
  ├─ Step 0: Preprocess & normalize text
  │
  ├─ Step 1: libpostal parse → extract town candidate
  │
  ├─ Step 2: GeoNames strict validation (country-scoped)
  │     ├─ match → ✅ validated (source=libpostal)
  │     └─ no match → unresolved
  │
  ├─ Step 3: GeoNames raw-address scan (country-scoped)
  │     ├─ match → ✅ validated (source=geonames_scan)
  │     └─ no match → LLM fallback queue
  │
  ├─ Step 4: LLM fallback (flagged rows only, temp=0, JSON schema)
  │
  ├─ Step 5: Final GeoNames re-validation
  │     ├─ match → ✅ validated (source=llm)
  │     ├─ town present but no match → ⚠️ needs_review
  │     └─ no town → ❌ rejected
  │
  └─ Output: Excel + metadata + audit log
```

**Key invariant:** No row is ever marked `validated` without a confirmed GeoNames match.

---

## 3. Design Rationale

| Decision | Rationale |
|----------|-----------|
| **Deterministic-first, LLM-last** | No labeled training data exists for CRF-based approaches (Conditional Random Field). libpostal provides strong multilingual parsing out-of-the-box without custom training. |
| **GeoNames as ground truth** | Prevents hallucinated or invented town names. Provides a verifiable, versioned reference dataset. |
| **Country-scoped matching** | Eliminates cross-country false positives (e.g., "Paris" in Texas vs. France, "Birmingham" in Alabama vs. England). |
| **LLM as guarded fallback** | Used only for rows where deterministic methods fail. Temperature 0 + JSON schema + post-validation ensures the LLM cannot introduce unverified data. |
| **Conservative default** | Any ambiguity defaults to `needs_review`, never to `validated`. This is appropriate for compliance-sensitive financial data. |

### GeoNames Coverage Choice

We use **`cities1000.txt`** (cities with population ≥ 1,000, ~166k entries) as the reference dataset. This provides near-complete town coverage for real-world postal addresses while keeping memory and ambiguity manageable. 

**Remaining coverage gaps:**
- Very small villages and hamlets with population < 1,000 will not match.
- Suburban neighborhoods and informal district names may not be present.

These gaps are acceptable: unmatched rows will route to LLM fallback and ultimately to `needs_review`, which is the safe default.

**Further upgrade path (if recall is still too low):**
- `allCountries.txt` (~12M entries) — **not recommended** without filtering, as it includes non-city features (mountains, rivers, parks, etc.) that would cause massive false positives and consume 5–10 GB of RAM. If needed, a filtered subset (feature_class=P only) could be extracted.

---

## 4. Tech Stack

| Category | Tool / Library | Purpose |
|----------|---------------|---------|
| **Runtime** | Python 3.11+ | Core language |
| **Data / Excel** | `pandas`, `openpyxl` | I/O for Excel workbooks |
| **Address parsing** | `postal` (libpostal bindings) | Multilingual address decomposition |
| **Fuzzy matching** | `rapidfuzz` | Near-match scoring in scan phase |
| **Normalization** | `Unidecode`, `regex` | Unicode transliteration, text normalization |
| **LLM fallback** | Ollama (`qwen2.5-coder:14b`) at `localhost:11434` | Guarded town extraction for unresolved rows |
| **HTTP client** | `requests` | Ollama API communication |
| **Validation** | `pydantic` | Schema enforcement for data contracts |
| **Testing** | `pytest` | Unit and integration tests |
| **Linting** | `ruff`, `black` | Code quality and formatting |

### LLM Configuration

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Model | `qwen2.5-coder:14b` | Good multilingual capability, runs locally |
| Temperature | `0` | Deterministic output — no creative sampling |
| Response format | Strict JSON schema | Prevents freeform text, enables programmatic parsing |
| Endpoint | `http://localhost:11434` | Local-only, no data leaves the machine |

---

## 5. Data Sources & Coverage

### Primary: GeoNames `cities1000.txt`

- **Path:** `data/reference/cities1000.txt`
- **Format:** TSV with columns: `geonameid`, `name`, `asciiname`, `alternatenames`, `latitude`, `longitude`, `feature_class`, `feature_code`, `country_code`, `admin1`, `population`, `elevation`, `timezone`, `modification_date`, etc.
- **Record count:** ~166,000 cities worldwide
- **Coverage floor:** Population ≥ 1,000

**How it is used:**

| Use Case | Description |
|----------|-------------|
| Strict validation | Exact match of town candidate against `name`, `asciiname`, `alternatenames` |
| Alternate-name matching | Multilingual support via the `alternatenames` field (comma-separated) |
| Deterministic scan | Token/phrase matching against the full raw address text |

> ⚠️ **Important:** `cities1000.txt` is a **gazetteer** (geographic dictionary), not labeled training data. It is used for validation, not model training.

### Versioning

The `modification_date` field in each GeoNames record indicates freshness. The specific snapshot used should be recorded in pipeline metadata for audit reproducibility.

---

## 6. Input / Output Contracts

### 6.1 Input Schema

| Column | Type | Constraints |
|--------|------|-------------|
| `address_1` | `string \| null` | Free-text address line |
| `address_2` | `string \| null` | Free-text address line |
| `address_3` | `string \| null` | Free-text address line |
| `country_code` | `string` | **Required.** ISO 3166-1 alpha-2 (e.g., `DE`, `JP`, `US`) |

### 6.2 Output Schema

| Column | Type | Description |
|--------|------|-------------|
| `town` | `string \| null` | Extracted town/city |
| `street` | `string \| null` | Extracted street name (best-effort from libpostal) |
| `building` | `string \| null` | Extracted building/house number (best-effort from libpostal) |
| `postal_code` | `string \| null` | Extracted postal/zip code (best-effort from libpostal) |
| `status` | `enum` | `validated` · `needs_review` · `rejected` |
| `confidence_score` | `float` | 0.0–1.0 |
| `parser_source` | `enum` | `libpostal` · `geonames_scan` · `llm` |
| `geonames_match` | `bool` | Whether the final town matched a GeoNames entry |
| `geonames_id` | `int \| null` | GeoNames ID of the matched city (for traceability) |
| `normalized_town` | `string \| null` | Normalized form used for matching |
| `warnings` | `list[string]` | Structured list of issues (see §10) |
| `review_reason` | `string \| null` | Human-readable explanation for `needs_review` / `rejected` |

#### Note on Secondary Fields (`street`, `building`, `postal_code`)

These fields are populated on a **best-effort basis** from libpostal's output. They are not subject to GeoNames validation (which only covers towns/cities). If libpostal does not produce a label for these fields, they remain `null`. The LLM fallback prompt does **not** attempt to extract these fields — its scope is limited to `town` resolution.

---

## 7. Processing Pipeline — Step by Step

### Step 0: Preprocessing

**Goal:** Normalize raw input into a consistent, matchable form.

1. **Concatenation strategy:**
   - Concatenate non-null address lines into `raw_address` with a space delimiter.
   - **Additionally**, preserve individual lines for positional heuristics:
     - In many formats, `address_3` (or the last non-null line) is more likely to contain city-level information.
     - If libpostal fails, the pipeline can attempt GeoNames matching on individual lines (last-to-first) as a secondary signal.
2. **Normalization:**
   - Unicode NFKC normalization
   - Casefolding for matching (preserve original for output)
   - Whitespace collapse (multiple spaces, tabs → single space)
   - Punctuation normalization (e.g., full-width to half-width)
3. **Audit:** Original input fields are preserved unmodified alongside processed output.

### Step 1: libpostal Parse

**Goal:** Extract structured components from the raw address using a pre-trained multilingual model.

1. Run `libpostal.parse_address()` on the concatenated `raw_address`.
2. Extract component candidates:
   - `city` → primary town candidate
   - `road` → street
   - `house_number` → building
   - `postcode` → postal code
   - `state`, `suburb`, `city_district` → secondary signals (used for disambiguation, not direct output)
3. Build `town_candidate`:
   - Prefer the explicit `city` label.
   - If multiple city-like labels exist, prefer the one appearing near postcode or administrative tokens.

> **Locale caveat:** libpostal's `city` label can be unreliable for some non-Western address formats (e.g., Japanese, Korean, Arabic). For these locales, the fallback to GeoNames scan (Step 3) and LLM (Step 4) becomes especially important. 

### Step 2: GeoNames Strict Validation

**Goal:** Confirm the town candidate against a trusted geographic reference, scoped to the correct country.

1. Filter GeoNames entries by `country_code`.
2. Attempt **exact normalized match** of `town_candidate` against:
   - `name` (primary name)
   - `asciiname` (ASCII transliteration)
   - `alternatenames` (comma-separated multilingual variants)
3. **On match:**
   - `status = validated`
   - `parser_source = libpostal`
   - `geonames_match = true`
   - Record `geonames_id` for traceability
4. **On no match:**
   - Row remains unresolved → proceeds to Step 3.

### Step 3: Deterministic GeoNames Raw-Address Scan

**Goal:** Catch town names that libpostal missed or mislabeled, by scanning the raw address text against the GeoNames lexicon.

1. Build a **country-filtered city lexicon** (all names + alternate names for the row's `country_code`).
2. Scan the full normalized `raw_address` for token/phrase matches against the lexicon.
3. **Acceptance rules (strict):**
   - Exact token or phrase match → accept.
   - Fuzzy match (via `rapidfuzz`) → accept only if score ≥ threshold **and** the match is unambiguous (single top candidate with clear margin).
   - If multiple candidates score similarly → **do not auto-validate** (ambiguity → unresolved).
4. **On accepted match:**
   - `status = validated`
   - `parser_source = geonames_scan`
   - `geonames_match = true`
5. **On no match or ambiguity:**
   - Row enters the **LLM fallback queue**.

### Step 4: LLM Fallback (Flagged Rows Only)

**Goal:** Use an LLM to propose a town candidate for rows that all deterministic methods failed to resolve.

**Prompt inputs:**
- `address_1`, `address_2`, `address_3`
- `country_code`
- Parser output and warnings from earlier stages
- Explicit instruction: _"Propose the most likely town name. Do not invent a name that is not implied by the address."_

**Controls:**

| Control | Value |
|---------|-------|
| Temperature | `0` |
| Response format | Strict JSON schema |
| Few-shot examples | Included in prompt |
| Max tokens | Bounded to prevent runaway responses |
| Decoding | Deterministic (greedy) |
| Concurrency | Up to `LLM_CONCURRENCY` (default 4) parallel requests per batch via `ThreadPoolExecutor` |

**Expected response schema:**

```json
{
  "town_candidate": "string | null",
  "confidence": 0.0,
  "needs_manual_review": false
}
```

### Step 5: Final GeoNames Re-Validation

**Goal:** Ensure the LLM's output is grounded in reality before accepting it.

Re-validation uses a **two-tier matching strategy:**

1. **Exact match first** — the LLM-proposed town candidate is checked against the GeoNames index (`name`, `asciiname`, `alternatenames`) using the same normalized exact-match logic as Step 2.
2. **Fuzzy match fallback** — if the exact match fails, a fuzzy match (`rapidfuzz.fuzz.partial_ratio`) is attempted. This catches cases where the LLM returns an abbreviated or partial name (e.g., `"St-Etienne"` vs. the official GeoNames name `"Court-Saint-Étienne"`).

#### Fuzzy Re-Validation Rules

| Rule | Detail |
|------|--------|
| Scorer | `partial_ratio` — finds the best substring alignment between candidate and GeoNames names |
| Threshold | Same as scan step: `FUZZY_MATCH_THRESHOLD` (default 92) |
| Ambiguity guard | Top candidates within `FUZZY_AMBIGUITY_MARGIN` (default 5 points) are considered tied |
| Disambiguation | When tied, the candidate whose GeoNames name has the most token overlap with the full `raw_address` wins |
| Short-name filter | GeoNames names ≤ 2 characters are excluded to prevent false positives |
| Confidence | Fuzzy-confirmed matches receive `CONFIDENCE_LLM_FUZZY_CONFIRMED` (0.70), lower than exact LLM matches (0.75) |

#### Outcome Matrix

| Outcome | Condition | Status | Source | Confidence |
|---------|-----------|--------|--------|------------|
| ✅ Exact match | LLM candidate matches GeoNames exactly | `validated` | `llm` | 0.75 |
| ✅ Fuzzy match | LLM candidate fuzzy-matches GeoNames unambiguously | `validated` | `llm` | 0.70 |
| ⚠️ Unverifiable | LLM proposed a town but no GeoNames match (exact or fuzzy) | `needs_review` | `llm` | 0.40 |
| ❌ No candidate | LLM returned `null` or flagged for manual review | `rejected` | — | 0.00 |

**Invariant:** No row is auto-validated without a GeoNames match, regardless of source.

---

## 8. Anti-Hallucination & Bias Controls

This pipeline handles compliance-sensitive financial data. Every design decision defaults toward **safety over recall**.

| # | Control | Description |
|---|---------|-------------|
| 1 | Deterministic-first | libpostal + GeoNames run before any LLM involvement. |
| 2 | LLM is never first-pass | The LLM only sees rows that deterministic methods could not resolve. |
| 3 | Temperature 0 | Eliminates creative/random sampling in LLM responses. |
| 4 | Strict JSON schema | Prevents freeform text responses; enforces parseable structure. |
| 5 | Mandatory post-LLM validation | Every LLM-proposed town is re-checked against GeoNames. |
| 6 | Country-scoped matching | All GeoNames lookups are filtered by `country_code` to prevent cross-country false positives. |
| 7 | Ambiguity → review | Ambiguous results default to `needs_review`, never `validated`. |
| 8 | Local-only LLM | Ollama runs on `localhost` — no address data leaves the machine. |

---

## 9. Confidence Score Policy

The confidence score is a **composite** reflecting how the town was resolved and how strong the match was.

### Score Components

| Component | Weight | Description |
|-----------|--------|-------------|
| GeoNames match quality | Primary | Exact primary name = 1.00 · Exact alternate/ASCII name = 0.95 · Fuzzy scan match = 0.80 |
| Parser signal | Secondary | libpostal heuristic confidence (when available) |
| LLM self-reported confidence | Capped | Used only after GeoNames confirms the match; never trusted alone |

### Hard Rule

> **If no GeoNames match exists, the row cannot have `status = validated`, regardless of confidence score.**

### Score Examples

| Scenario | Score | Status |
|----------|-------|--------|
| libpostal → exact GeoNames primary name match | 1.00 | `validated` |
| libpostal → exact alternate name match | 0.95 | `validated` |
| GeoNames scan → fuzzy match above threshold | 0.80 | `validated` |
| LLM → exact GeoNames match | 0.75 | `validated` |
| LLM → fuzzy GeoNames match (unambiguous) | 0.70 | `validated` |
| LLM → no GeoNames match (exact or fuzzy) | 0.40 | `needs_review` |
| No town candidate from any source | 0.00 | `rejected` |

---

## 10. Error Handling & Resilience

### Input Validation

| Condition | Action |
|-----------|--------|
| Missing `country_code` | Row rejected with `review_reason = "missing_country_code"` |
| All address fields null | Row rejected with `review_reason = "no_address_data"` |
| Invalid `country_code` format | Row rejected with `review_reason = "invalid_country_code"` |

### LLM Fallback Failures

| Condition | Action |
|-----------|--------|
| Ollama endpoint unreachable | Retry up to 3 times with exponential backoff; then mark row as `needs_review` with `review_reason = "llm_unavailable"` |
| LLM returns malformed JSON | Retry once with stricter prompt; then `needs_review` with `review_reason = "llm_parse_error"` |
| LLM timeout | Configurable timeout (default: 30s); then `needs_review` with `review_reason = "llm_timeout"` |

### Warning Taxonomy

Warnings are structured as a list of strings. Common warning values:

| Warning | Meaning |
|---------|---------|
| `libpostal_no_city_label` | libpostal did not produce a `city` component |
| `multiple_town_candidates` | Ambiguous — multiple plausible towns detected |
| `fuzzy_match_below_threshold` | Scan found a near-match but below acceptance threshold |
| `llm_low_confidence` | LLM self-reported low confidence |
| `geonames_no_match` | Town candidate did not match any GeoNames entry |
| `country_code_mismatch_suspected` | Address content suggests a different country than provided |

---

## 11. Performance & Scalability

### GeoNames Loading Strategy

- **On startup:** Load `cities1000.txt` once into an **in-memory, country-indexed dictionary**.
  - Key: `country_code` → Value: set of normalized names (primary + ASCII + alternates).
  - Estimated memory footprint: ~200–400 MB depending on alternate name expansion.
- **Lookup complexity:** O(1) per country filter, O(1) per exact match (set lookup), O(n) per fuzzy scan (within country scope).
- **Alternate approach (future):** If memory becomes a concern, consider SQLite or a trie-based index.

### Batch Processing & LLM Concurrency

| Parameter | Default | Env Var | Notes |
|-----------|---------|---------|-------|
| Input batch size | Entire file | — | For v1, process full Excel file in one pass |
| LLM batch size | 10 rows | `LLM_BATCH_SIZE` | Unresolved rows are grouped into batches |
| LLM concurrency | 4 threads | `LLM_CONCURRENCY` | Within each batch, requests are sent in parallel via `ThreadPoolExecutor` (range: 1–16) |
| LLM timeout per row | 30 seconds | `LLM_TIMEOUT_SECONDS` | Configurable via environment variable |
| LLM max retries | 3 | — | Exponential backoff: 1s, 2s, 4s |

#### How LLM Batching Works

Unresolved rows are divided into batches of `LLM_BATCH_SIZE` (default 10). Within each batch, up to `LLM_CONCURRENCY` (default 4) HTTP requests are sent to Ollama **concurrently** using a `ThreadPoolExecutor`. This parallelism significantly reduces wall-clock time for LLM-heavy workloads:

- **Sequential** (old): 10 rows × 3–4s each ≈ 30–40s per batch
- **Concurrent** (4 threads): 10 rows ÷ 4 threads × 3–4s ≈ 8–10s per batch

Each thread makes its own `requests.post()` call with independent retry logic. Ollama handles concurrent requests via its built-in request queue. If any individual LLM call raises an unexpected exception, it is caught and the row is marked `needs_review` with an `llm_unavailable` warning — it does not affect other rows in the batch.

> **Tuning note:** Set `LLM_CONCURRENCY=1` to restore sequential behaviour. Increase towards 8–16 if running against a GPU-backed Ollama instance that can serve multiple requests efficiently.

### Expected Throughput (Estimates)

| Stage | Speed |
|-------|-------|
| Preprocessing + libpostal | ~1,000 rows/sec |
| GeoNames exact match | ~10,000 rows/sec (in-memory lookup) |
| GeoNames fuzzy scan | ~100–500 rows/sec (depends on lexicon size per country) |
| LLM fallback (sequential) | ~1–5 rows/sec (local inference, model-dependent) |
| LLM fallback (4 threads) | ~4–15 rows/sec (limited by model throughput) |

The pipeline is designed so that the vast majority of rows resolve in the fast deterministic stages, with only a small fraction reaching the LLM.

---

## 12. Implementation Plan

### Phase 1: Foundation

| Task | Deliverable |
|------|-------------|
| Project scaffold, configs, environment setup | Runnable project skeleton |
| Excel I/O with `pandas` + `openpyxl` | Read input, write output template |
| Input schema validation with `pydantic` | Reject malformed rows early |
| Normalization utilities (Unicode, whitespace, punctuation) | `preprocess.py` module |

**Exit criterion:** CLI reads input Excel, validates schema, writes output template with empty structured columns.

### Phase 2: GeoNames Core

| Task | Deliverable |
|------|-------------|
| Load and parse `cities1000.txt` | `geonames_loader.py` |
| Build country-indexed lookup maps (`name`, `asciiname`, alternates) | `geonames_matcher.py` |
| Implement strict exact match | Validation function |
| Implement deterministic raw-address scan with fuzzy matching | `geonames_scan.py` |

**Exit criterion:** Tested GeoNames validation module with unit tests covering exact match, alternate name match, fuzzy match, and country scoping.

### Phase 3: libpostal Integration

| Task | Deliverable |
|------|-------------|
| Install and wrap `postal` bindings | `parser_libpostal.py` |
| Candidate extraction rules (city, road, postcode, etc.) | Extraction logic |
| Wire libpostal output → GeoNames validation path | End-to-end deterministic flow |

**Exit criterion:** Deterministic end-to-end baseline (no LLM) that correctly processes a test set of multilingual addresses.

### Phase 4: LLM Fallback Integration

| Task | Deliverable |
|------|-------------|
| Ollama client with retry/timeout logic | `llm_ollama.py` |
| Prompt engineering with JSON schema enforcement | Prompt templates |
| Few-shot example curation | Example set |
| Post-LLM GeoNames re-validation | Final validation path |
| Decision engine: `validated` / `needs_review` / `rejected` | `decision_engine.py` |

**Exit criterion:** Full production flow with guarded LLM fallback, processing a test set end-to-end.

### Phase 5: Testing & Hardening

| Task | Deliverable |
|------|-------------|
| Unit tests for matching, scoring, status logic | `tests/` suite |
| End-to-end tests with multilingual address samples | Integration test set |
| Edge case coverage (empty fields, ambiguous matches, LLM failures) | Robustness tests |
| Audit logging and metadata output | Reproducibility |
| Performance benchmarking | Throughput report |

**Exit criterion:** Stable release candidate with documented test results and performance characteristics.

---

## 13. Project Structure

```
structured_address_ai_poc/
├── docs/
│   └── DESIGN.md
├── data/
│   ├── reference/
│   │   └── cities1000.txt
│   ├── samples/                    # Test input files
│   │   └── test_addresses.xlsx
│   └── output/                     # Pipeline output files
│       └── .gitkeep
├── src/
│   ├── __init__.py
│   ├── config.py                   # Environment variables, paths, thresholds
│   ├── pipeline.py                 # Top-level orchestration
│   ├── io_excel.py                 # Excel read/write
│   ├── preprocess.py               # Text normalization
│   ├── parser_libpostal.py         # libpostal wrapper
│   ├── geonames_loader.py          # Load and index cities1000.txt
│   ├── geonames_matcher.py         # Exact match validation
│   ├── geonames_scan.py            # Deterministic raw-address scan
│   ├── llm_ollama.py               # Ollama client with retry logic
│   ├── decision_engine.py          # Status assignment logic
│   └── schemas.py                  # Pydantic models for I/O contracts
├── tests/
│   ├── test_preprocess.py
│   ├── test_geonames_matcher.py
│   ├── test_geonames_scan.py
│   ├── test_decision_engine.py
│   └── test_pipeline_e2e.py
├── requirements.txt
└── README.md
```

---

## 14. Design Decisions

Key design choices made during implementation, with rationale for future reference.

| # | Decision | Resolution | Rationale |
|---|----------|------------|-----------|
| 1 | **Fuzzy match threshold** | 92/100 (`FUZZY_MATCH_THRESHOLD`), ambiguity margin 5 (`FUZZY_AMBIGUITY_MARGIN`). Configurable via env vars. | Empirically balanced — 96 was too strict for transliterated names; 92 catches variants like `"St-Etienne"` → `"Court-Saint-Étienne"` without false positives. |
| 2 | **Admin hierarchy disambiguation** | Deferred to v2. v1 uses flat city-name matching with population-based tiebreaking. | Flat matching covers >95% of real-world addresses. Admin hierarchy adds complexity with diminishing returns for POC scope. |
| 3 | **GeoNames dataset tier** | `cities1000.txt` (~166k entries, pop ≥ 1,000). `allCountries.txt` rejected. | Upgraded from `cities5000.txt` for +2 validated rows on test set (46% → 62%). `allCountries.txt` includes non-city features and would consume 5–10 GB RAM. |
| 4 | **LLM batch size, concurrency, and timeout** | Batch=10, concurrency=4 threads (`ThreadPoolExecutor`), timeout=30s per row. All configurable via env vars. | 4 concurrent threads balance throughput with local Ollama capacity. Env vars allow tuning per deployment. |
| 5 | **Warnings format** | `list[string]` with predefined taxonomy (see §10). | Simple, machine-readable, extensible. Avoids over-engineering structured warning objects for POC. |
| 6 | **libpostal installation method** | System-level build (C library + Python `postal` bindings). | Docker adds deployment complexity without benefit for single-machine POC. System build is stable on macOS/Linux. |

---

## 15. Immediate Next Tasks

| Priority | Task | Depends On |
|----------|------|------------|
| 🔴 P0 | Implement GeoNames loader + country-indexed matcher | — |
| 🔴 P0 | Implement libpostal parser wrapper | libpostal installed |
| 🟡 P1 | Implement decision engine (`validated` / `needs_review` / `rejected`) | Matcher |
| 🟡 P1 | Build Excel I/O pipeline with pydantic schema validation | — |
| 🟢 P2 | Implement LLM fallback module with strict schema + final re-validation | Decision engine |
| 🟢 P2 | Curate multilingual test address set for benchmarking | — |

---

## Appendix A: GeoNames `cities1000.txt` Column Reference

| Index | Column | Description |
|-------|--------|-------------|
| 0 | `geonameid` | Integer ID |
| 1 | `name` | Name in UTF-8 |
| 2 | `asciiname` | ASCII transliteration |
| 3 | `alternatenames` | Comma-separated alternate names |
| 4 | `latitude` | Decimal degrees |
| 5 | `longitude` | Decimal degrees |
| 6 | `feature_class` | GeoNames feature class (P = populated place) |
| 7 | `feature_code` | Detailed feature code (PPL, PPLA, PPLC, etc.) |
| 8 | `country_code` | ISO 3166-1 alpha-2 |
| 9 | `cc2` | Alternate country codes |
| 10 | `admin1_code` | First-level admin division |
| 11 | `admin2_code` | Second-level admin division |
| 12 | `admin3_code` | Third-level admin division |
| 13 | `admin4_code` | Fourth-level admin division |
| 14 | `population` | Population count |
| 15 | `elevation` | Elevation in meters |
| 16 | `dem` | Digital elevation model value |
| 17 | `timezone` | IANA timezone |
| 18 | `modification_date` | Last modification date (YYYY-MM-DD) |
