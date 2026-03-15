# Test Services

Unit tests for every service in the address-resolution pipeline. Each test script
isolates one step (or supporting module) and validates it independently using
mocked dependencies — no database, no network, no LLM calls required.

---

## Pipeline Overview

```
┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐
│ Step 0  │──▶│ Step 1  │──▶│ Step 2  │──▶│ Step 3  │──▶│ Step 4  │──▶│ Step 5  │──▶│ Step 6  │──▶│ Step 7  │──▶│ Step 8  │
│Normalize│   │ Parse   │   │ Postal  │   │ Exact   │   │Mismatch │   │  Scan   │   │  LLM    │   │Revalid. │   │ Persist │
└─────────┘   └─────────┘   └─────────┘   └─────────┘   └─────────┘   └─────────┘   └─────────┘   └─────────┘   └─────────┘
```

---

## Test Scripts by Pipeline Step

### Step 0 — `test_normalizer.py`

**Service:** `services.normalizer` — Text preprocessing and normalization.

Validates every normalization primitive that prepares raw address text before it
enters the pipeline:

| Test class | What it checks |
|---|---|
| `TestNormalizeUnicode` | NFKC normalization, ASCII passthrough |
| `TestCollapseWhitespace` | Multi-space, tab/newline collapsing, trim |
| `TestNormalizePunctuation` | Full-width → ASCII comma/period, ideographic comma |
| `TestCasefold` | Upper → lower, German ß expansion |
| `TestToAscii` | Accented char → ASCII (unidecode) |
| `TestNormalizeForMatching` | End-to-end normalization chain |
| `TestTokenize` | Whitespace splitting, empty-string filtering |
| `TestExtractNgrams` | Unigram/bigram generation, edge cases |
| `TestBuildRawAddress` | Concatenation of address_1/2/3, None/empty handling |
| `TestRedactPii` | Long-string truncation for logging |
| `TestPreprocess` | Full `preprocess(state)` → `raw_address` assembly |

---

### Step 1 — `test_libpostal_parser.py`

**Service:** `services.libpostal_parser` — Address parsing via libpostal C library.

Tests the `parse()` function which extracts structured fields (city, street,
building, postal code) from a raw address string. Libpostal is mocked so tests
run without the native C library installed.

| Test class | What it checks |
|---|---|
| `TestParseWithoutLibpostal` | Graceful degradation when libpostal unavailable; empty/whitespace input |
| `TestParseWithMockedLibpostal` | City/building extraction, missing labels, multiple candidates, parse errors, empty values |

---

### Step 2 — `test_postal_lookup.py`

**Service:** `services.postal_lookup` — Disambiguation signal extraction from postal-code DB.

Queries the postal-codes table for the parsed postal code and extracts an
`admin1_code` that is used downstream (Step 3) for disambiguation.

| Test class | What it checks |
|---|---|
| `TestPostalLookup` | admin1 extraction, no-results handling, missing postal code skip, missing admin fields |

---

### Step 3 — `test_geonames_exact.py`

**Service:** `services.geonames_exact` — Exact city matching with disambiguation.

The core resolution step: looks up the parsed city name in GeoNames and
disambiguates when multiple cities share the same name (e.g. Springfield) using
the postal signal from Step 2.

| Test class | What it checks |
|---|---|
| `TestDisambiguate` | admin1 selection, population fallback, single match, empty set, case-insensitive |
| `TestMatchWithDisambiguation` | Full `match()` with postal signal, no signal, unique city, no match, alternate name preservation |

---

### Step 4 — `test_mismatch_detector.py`

**Service:** `services.mismatch_detector` — Country-code mismatch detection.

After exact matching, this step checks whether the resolved town actually exists
in the stated country. If not, it flags a mismatch and suggests the correct
country code.

| Test class | What it checks |
|---|---|
| `TestMismatchDetector` | Exact-match skip, no candidate skip, town-not-found, mismatch flagging, no mismatch when valid, highest-population preference |

---

### Step 5 — `test_address_scanner.py`

**Service:** `services.address_scanner` — Raw address token scanning fallback.

When earlier steps fail to resolve a city, the scanner tokenizes the raw address
into n-grams and matches them against all known city names for the given country.

| Test class | What it checks |
|---|---|
| `TestScan` | Exact token match, no match, empty inputs, longest n-gram preference, ambiguous short-match skipping |
| `TestFuzzyScan` | Below-threshold rejection, short n-gram filtering |

---

### Step 6 — `test_llm_parser.py`

**Service:** `address_pipeline_agent.sub_agents.llm_parser.agent` — LLM-based town resolution.

Tests the LLM agent that resolves cities that earlier deterministic steps could
not match. All LLM calls are mocked (via `litellm`), and GeoNames tool functions
are patched via `patch.dict(_TOOL_FUNCTIONS, ...)` so no database or network is
required.

| Test class | What it checks |
|---|---|
| `TestParseLlmText` | JSON extraction from clean text, fenced blocks, embedded prose, empty/no-JSON input, nested fences |
| `TestDetectTextToolCall` | Text-emitted tool-call detection (Ollama quirk), final-answer vs tool-call, unknown tool, missing keys, non-dict |
| `TestExecuteToolCall` | Tool dispatch via `_TOOL_FUNCTIONS` dict, unknown tool error, exception wrapping |
| `TestBuildInstruction` | System prompt construction: field injection, sparse state defaults, mismatch flag propagation |
| `TestLlmParserAgentCleanJson` | Full agent happy path with clean JSON, fenced JSON response |
| `TestLlmParserAgentToolCalls` | Native tool call → answer flow, text-emitted tool call → answer flow |
| `TestLlmParserAgentEdgeCases` | Empty response, LLM exception, `town_candidate` key remapping, `set_model_response` wrapper, country-code mismatch propagation, multi-turn token accumulation |
| `TestLlmAddressOutput` | Pydantic schema validation: confidence clamping, None→empty coercion, suggested country code, non-numeric confidence |

---

### Step 7 — `test_geonames_revalidation.py`

**Service:** `services.geonames_revalidation` — Re-validation of LLM results.

After the LLM suggests a town, this step validates the suggestion against the
GeoNames database to assign a confidence score and detect cross-country
mismatches.

| Test class | What it checks |
|---|---|
| `TestRevalidateDeterministic` | Rows already resolved pass through unchanged |
| `TestRevalidateLLM` | No LLM result, empty town, exact match, suggested-country match, no match anywhere, fuzzy match, cross-country fallback, postal fallback |
| `TestPreferAddressSpelling` | Address-text spelling preferred over LLM spelling, edge cases |

---

### Step 8 — `test_persistence.py`

**Service:** `services.persistence` — Final result assembly.

The last pipeline step: assembles the final output dict from the accumulated
state, maps internal statuses to output statuses, rounds confidence, joins
warnings, and computes review reasons.

| Test class | What it checks |
|---|---|
| `TestPersist` | Validated/resolved/needs_review/rejected status mapping, LLM-usage zeros, warning joining, geonames_match flag, mismatch info, confidence rounding |
| `TestComputeReviewReason` | Validated → None, no address data, LLM result present, no LLM result, other status |

---

## Supporting Services (not pipeline steps)

### `test_geonames_repo.py`

**Service:** `services.geonames_repo` — SQLite data-access layer for GeoNames.

Shared repository used by Steps 3–7. Tests use an **in-memory SQLite database**
seeded with sample data — no external DB files needed.

| Test class | What it checks |
|---|---|
| `TestRowsToDicts` | Row-to-dict conversion |
| `TestQueryCity` | City lookup, case-insensitivity, alternate names |
| `TestListCountriesForCity` | Cross-country city lookup |
| `TestQueryPostalCode` | Postal-code lookup |
| `TestSearchPostalByPlaceName` | Place-name search, empty input |
| `TestGetAllNormalizedNames` | Full name-set retrieval for a country |

---

### `test_io_reader.py`

**Service:** `services.io_reader` — CSV/Excel input file reading.

Validates file ingestion, column-name normalization (aliases like `addr_1` →
`address_1`), country-code validation, and edge cases like Namibia's `NA` code.

| Test class | What it checks |
|---|---|
| `TestClean` | String cleaning: whitespace, None, NaN |
| `TestReadInputCSV` | CSV reading, column aliases, invalid codes, uppercasing, Namibia NA |
| `TestReadInputExcel` | XLSX reading |
| `TestReadInputErrors` | File not found, unsupported format, missing columns |

---

### `test_io_writer.py`

**Service:** `services.io_writer` — CSV/Excel output file writing.

Validates output generation, column ordering, parent-directory creation, and
empty-result handling.

| Test class | What it checks |
|---|---|
| `TestWriteCSV` | CSV writing, column order |
| `TestWriteExcel` | XLSX writing, custom sheet name |
| `TestWriteOutputEdgeCases` | Parent dir creation, empty results, extra columns, resolved path |

---

## Integration Test

### `test_single_row_cost.py`

**Full pipeline** (Steps 0–8) end-to-end via ADK Runner with a real LLM call
(Ollama). Processes a single address through every step and reports per-step
timing and token costs. **Requires a running Ollama instance.**

---

## Configurable Test Data

Every test script has a **`SAMPLE ADDRESSES`** section at the top (marked with a
box-drawn border). To test with your own addresses, edit only those constants —
no changes to test logic required.

```python
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with your own data               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

SAMPLE_ADDRESS_1 = "123 Main St"
SAMPLE_COUNTRY_CODE = "US"
# ...
```

---

## Running Tests

### Flags

| Flag | Purpose |
|------|---------|
| `-v` | **Verbose** — shows each test name and PASSED / FAILED |
| `-s` | **Show output** — disables pytest's output capture so `print()` / `report()` diagnostics are visible. **Without `-s` you will only see PASSED or FAILED; with `-s` you will see the actual input → output data at every step.** |

> **The `%` value** (e.g. `PASSED [ 27%]`) is simply pytest's **progress indicator** — it shows what fraction of the total collected tests have been executed so far. It is _not_ a score or confidence metric.

### Commands

```bash
# All service tests (names only)
pytest tests/test_services/ -v

# All service tests WITH diagnostic output (recommended)
pytest tests/test_services/ -v -s

# Single step with diagnostics
pytest tests/test_services/test_normalizer.py -v -s

# Skip integration test (needs Ollama)
pytest tests/test_services/ -v -s --ignore=tests/test_services/test_single_row_cost.py

# Save results with a datetime stamp (with diagnostics)
TIMESTAMP=$(date +"%Y%m%d_%H%M%S") && \
  pytest tests/test_services/test_normalizer.py -v -s 2>&1 \
  | tee tests/test_results/test_normalizer_${TIMESTAMP}.txt

# Run ALL test scripts and save each result individually
TIMESTAMP=$(date +"%Y%m%d_%H%M%S") && \
  for f in tests/test_services/test_*.py; do \
    name=$(basename "$f" .py); \
    pytest "$f" -v -s 2>&1 | tee "tests/test_results/${name}_${TIMESTAMP}.txt"; \
  done
```

### Diagnostic Output

Every test method calls the shared `report()` helper (defined in
`tests/test_services/report.py`) to print a structured block showing the
**input** fed to the function and the **output** it produced.  Example:

```
  ┌─ NORMALIZE_UNICODE
  │  input: 'ﬁ'
  │  output: 'fi'
  └────────────────────────────────────────────────────────────
```

This output only appears when you run with the **`-s`** flag.
