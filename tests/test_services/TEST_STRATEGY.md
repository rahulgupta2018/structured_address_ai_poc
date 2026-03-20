# Test Strategy — Address Pipeline Services

Unit tests for every service in the address-resolution pipeline. Each test script
isolates one step (or supporting module) and validates it independently using
mocked dependencies — no database, no network, no LLM calls required.

---

## Pipeline Overview

```
┌─────────┐   ┌─────────┐   ┌──────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐
│ Step 0  │──▶│ Step 1  │──▶│ Country- │──▶│ Step 2  │──▶│ Step 3  │──▶│ Step 4  │──▶│ Step 5  │──▶│ Step 6  │──▶│ Step 7  │──▶│ Step 8  │
│Normalize│   │ Parse   │   │ only     │   │ Postal  │   │ Exact   │   │Mismatch │   │  Scan   │   │  LLM    │   │Revalid. │   │ Persist │
└─────────┘   └─────────┘   │ guard    │   └─────────┘   └─────────┘   └─────────┘   └─────────┘   └─────────┘   └─────────┘   └─────────┘
                             └──────────┘
```

**Four flows:** Flow 0 (0→1→guard→8), Flow 1 (0→1→2→3→8), Flow 2 (0→1→2→3→4→5→8), Flow 3 (0→1→2→3→4→5→6→7→8)

**Key guards:**
- **Country-only guard** (after Step 1): If address contains only a country name with no other components → `needs_review` with `country_only_address` warning, skips Steps 2–7
- **Step 3 postal-code fallback**: Towns absent from cities500 but present in postal-codes dataset (e.g. Taxila/PK) → `match_type="postal"`, `confidence=0.95`
- **Step 8 ASCII normalization**: `town` and `normalized_town` fields are ASCII-normalized via `to_ascii()` (e.g. Brasília→Brasilia)

---

## Test Scripts by Pipeline Step

### Step 0 — `test_normalizer.py`

**Service:** `services.normalizer` — Text preprocessing and normalization.

Validates every normalization primitive that prepares raw address text before it enters the pipeline. Each test class covers both **happy-path** and
**negative/edge-case** scenarios (marked `[Negative]` / `[Edge]` in docstrings).

| Test class | Happy path | Negative / edge cases |
|---|---|---|
| `TestNormalizeUnicode` | NFKC ligature decomposition (ﬁ→fi), ASCII passthrough | Empty string |
| `TestCollapseWhitespace` | Multi-space collapse, tab/newline collapse, leading/trailing trim | Empty string, whitespace-only input |
| `TestNormalizePunctuation` | Full-width → ASCII comma/period, ideographic comma, regular punctuation passthrough | Empty string, mixed fullwidth in realistic Japanese address |
| `TestCasefold` | Upper → lower, German ß → ss expansion | Empty string, already-lowercase (idempotent) |
| `TestToAscii` | Accented chars (café→cafe), umlauts (München→Munchen) | Empty string, CJK transliteration (東京都→pinyin), Arabic script (no crash), emoji stripped |
| `TestNormalizeForMatching` | End-to-end chain: accented+mixed-case+whitespace, fullwidth+unicode | Empty string, whitespace-only |
| `TestTokenize` | Basic two-word split, double-space filtering | Empty string, whitespace-only, single token |
| `TestExtractNgrams` | Unigram+bigram generation, max_n > token count | Empty token list, single token (no bigrams) |
| `TestBuildRawAddress` | Three-line concat, skip empty/None, strip whitespace | All-empty lines, whitespace-only lines, empty list |
| `TestRedactPii` | Long string redacted, short string with ellipsis | Empty → `<empty>`, None → `<empty>`, boundary (exactly 5 chars), whitespace-only → `<empty>` |
| `TestPreprocess` | Full `preprocess(state)` → raw_address + normalized, None/empty line skip, preserves extra state keys | All-None lines (warning), whitespace-only lines (warning), missing keys entirely (no crash) |

**55 total tests** (33 happy-path + 22 negative/edge-case). All call the real service functions — zero mocks.

---

### Step 1 — `test_libpostal_parser.py`

**Service:** `services.libpostal_parser` — Address parsing via libpostal C library.

Tests the `parse()` function which extracts structured fields (city, street,
building, postal code) from a raw address string, plus the `_country_name_to_code`
helper. Libpostal's C library is mocked so all tests run without the native
dependency installed.

| Test class | Happy path | Negative / edge cases |
|---|---|---|
| `TestParseLibpostalUnavailable` | Returns defaults and warns `libpostal_not_installed` | [Edge] Preserves existing state keys |
| `TestParseEmptyAddress` | — | Empty string, whitespace-only, missing `raw_address` key |
| `TestParseSingleCity` | Standard address (town, street, building, country), `suburb` label, `city_district` label | — |
| `TestParseMultipleCandidates` | Last explicit `city` label wins | [Edge] No `city` label among candidates → fallback to first |
| `TestParseNoCityLabel` | — | No city/suburb/city_district labels → town is None, warning |
| `TestParseError` | — | RuntimeError from libpostal → `libpostal_parse_error` warning |
| `TestParseEmptyValues` | — | [Edge] Empty/whitespace token values ignored, fields stay None |
| `TestParseCountryMismatch` | No mismatch when country matches `country_code` | Mismatch flagged (Germany vs US), [Edge] no country token, unknown country name, no `country_code` in state |
| `TestCountryNameToCode` | Known country → ISO code, case-insensitive | Unknown country → None |
| `TestRealLibpostalParsing` | _(optional, skipped if C library not installed)_ — end-to-end with real libpostal | — |

**21 mocked tests** (8 happy-path + 7 negative + 6 edge-case) + 1 optional
real-libpostal integration test. All mocked tests patch only the external C
library call; the `parse()` wrapper logic executes fully.

---

### Step 2 — `test_postal_lookup.py`

**Service:** `services.postal_lookup` — Disambiguation signal extraction from postal-code DB.

`lookup(state)` reads `libpostal_postal_code`, `country_code`, and optionally
`suggested_country_code` (from Step 1 mismatch detection) from state, then uses
a **3-tier fallback strategy** to resolve the postal code:

1. **Primary** — `postal_code + country_code` (direct lookup)
2. **Suggested-country** — `postal_code + suggested_country_code` (when primary
   returns nothing and Step 1 detected a country mismatch)
3. **Postal-only** — `postal_code` without country filter, then disambiguated by
   cross-referencing with `suggested_country_code`, or accepted if all results
   point to a single country. Multi-country ambiguous results are rejected.

Writes five output fields: `postal_town_candidate`, `postal_admin1_code`,
`postal_region`, `postal_city_hint`, and `postal_lookup_method` (which tier
resolved: `"primary"`, `"suggested_country"`, `"postal_only"`, or `None`).

All tests call the **real** `lookup()` against the **real** SQLite database —
zero mocks. Generic parameterized test classes drive multiple sample postal codes
through the same assertions.

| Test class | Happy / Negative / Edge | What it checks | Expected behaviour |
|---|---|---|---|
| `TestLookupHappyPath` | Happy (×6 samples) | Parameterized: US 62701, IT 08042, TH 81150, DE 10115, JP 100-0001, AU 2026 | Each sample checks `postal_town_candidate`, `postal_admin1_code`, `postal_region`, `postal_city_hint`, and `postal_lookup_method="primary"` |
| `TestLookupNoResults` | Negative (×1 sample) | Non-existent postal 00000/US | All output fields are `None`, `postal_lookup_method=None` |
| `TestLookupMissingInputs` | Negative (×2 samples) | Parameterized: `None` postal, empty state | Early return, all output fields default to `None` |
| `TestLookupWhitespace` | Edge | Postal code with leading/trailing spaces → still matches | `query_postal_code` strips whitespace; same result as clean input |
| `TestLookupPreservesState` | Edge | Pre-existing keys in state survive after lookup | `extra_key`, `row_index` etc. are not removed |
| `TestLookupFromRawAddress` | E2E (×4 samples) | Parameterized: raw address → `parse()` → `lookup()` pipeline | Pakistan/Japan (no postal) → None; Italy 08042 → Bari Sardo; Thailand 81150 → Ko Lanta |
| `TestLookupSuggestedCountryFallback` | Happy (×3 samples) | Parameterized: 62701+DE/suggested=US, 81150+US/suggested=TH, 10115+XX/suggested=DE | Wrong CC with Step 1 suggested CC → resolves via `"suggested_country"` tier |
| `TestLookupPostalOnlyFallback` | Happy + Edge (×1 sample + 1 edge) | Parameterized: 62701 with empty CC (globally unambiguous); Edge: 62701+DE without suggested CC | Resolves via `"postal_only"` since 62701 exists only in US |
| `TestLookupPostalOnlyAmbiguous` | Negative (×2 samples) | Parameterized: 81150+US (4 countries), 10115+XX (7 countries) — no suggested CC | All output fields are `None`, multi-country ambiguity rejected |

**65 tests** (30 happy-path parameterized + 5 negative + 2 edge-case +
12 suggested-country + 4 postal-only + 12 end-to-end parameterized),
all hitting the real GeoNames SQLite database — zero mocks.

---

### Step 3 — `test_geonames_exact.py`

**Service:** `services.geonames_exact` — Exact city matching with disambiguation.

The core resolution step: looks up the parsed city name in GeoNames and
disambiguates when multiple cities share the same name (e.g. Springfield) using
the postal signal from Step 2. `match(state)` reads `libpostal_town`,
`libpostal_city_candidates`, `postal_town_candidate`, `postal_admin1_code`,
`postal_region`, `country_code`, `mismatch_detected`, and
`suggested_country_code` from state, then writes `exact_match`,
`geonames_id`, `town_candidate`, `match_type`, `match_confidence`, and
`matched_country_code`.

When `mismatch_detected` is set, Step 3 tries `suggested_country_code` FIRST,
then falls back to `country_code`. This multi-country resolution prevents
false positives from wrong-CC addresses (e.g. Thai address with CC=US now
resolves to the correct Thai city instead of an accidental US match).

After the main candidate loop fails, a **postal-code fallback** tries
libpostal-derived candidates against the GeoNames postal-codes table
(country-filtered). This catches towns too small for cities500 (e.g.
Taxila/PK) that exist only in the postal dataset. `match_type="postal"`,
`confidence=0.95`. Skips `postal_town_candidate` to avoid circular matches.

All tests call the **real** `match()` and `_disambiguate()` against the **real**
SQLite database — zero mocks. Generic parameterized test classes drive multiple
sample cities through the same assertions.

| Test class | Happy / Negative / Edge | What it checks | Expected behaviour |
|---|---|---|---|
| `TestDisambiguateHappyPath` | Happy (×4 samples) | Parameterized: admin1=IL, admin1=MA select correct Springfield; no postal signal → population fallback (MO); single match returns it | `_disambiguate()` returns the correct city dict based on admin1 or population |
| `TestDisambiguateNegative` | Negative (×3 samples) | Parameterized: empty list, unknown admin1 code, None admin1 | Empty → `{}`, unknown admin1 → population fallback, None → population fallback |
| `TestMatchHappyPath` | Happy (×5 samples) | Parameterized: Springfield IL (disambiguated), Berlin DE (unique), San Francisco CA (unique), Ko Lanta TH (unique), Bari Sardo IT (postal fallback candidate) | `exact_match=True`, correct `geonames_id`, `match_type=primary`, `match_confidence=1.0` |
| `TestMatchAlternateName` | Happy (×1) | Morbi IN — alternate name resolves, input spelling preserved | `exact_match=True`, `match_type=alternate`, `match_confidence=0.95`, `town_candidate=Morbi` (not "Morvi") |
| `TestMatchNoResults` | Negative (×2 samples) | Parameterized: non-existent city (Xyzzyville), missing candidates (empty state) | `exact_match=False`, `geonames_id=None`, `match_confidence=0.0` |
| `TestMatchCandidateFallback` | Edge (×1) | `libpostal_town` has no match but `postal_town_candidate` does → falls back to postal candidate | `exact_match=True` via postal candidate, correct Springfield IL geonames_id |
| `TestMatchPreservesState` | Edge (×1) | Pre-existing keys survive after `match()` | `extra_key`, `row_index`, `job_id` not removed |
| `TestMatchWrongCountryCode` | Edge (×2) | Thai address + US country code → suggested-CC-first resolves Krabi in TH (not US false positive); London address + AU country code → resolves London in GB via suggested CC | `exact_match=True`, correct `geonames_id` in suggested country, `mismatch_detected=True`, `matched_country_code` = suggested CC |
| `TestMatchCityNotInDB` | Negative (×2) | Taxila (real city, absent from GeoNames cities500) — postal fallback resolves when libpostal provides city candidate; short address form where libpostal cannot extract city → no match | Postal fallback: `exact_match=True`, `match_type="postal"`, `confidence=0.95`; short form: `exact_match=False`, "jhang" stays in `libpostal_street` not `libpostal_city_candidates` |
| `TestMatchFromRawAddress` | E2E (×5 samples) | Parameterized: raw address → `parse()` → `lookup()` → `match()` full pipeline flow | Italy/Thailand/US addresses with postal codes resolve; Pakistan/Japan addresses without postal codes get no match |

**61 tests** (29 happy-path parameterized + 7 negative + 5 edge-case +
20 end-to-end parameterized), all hitting the real GeoNames SQLite database
and real libpostal parser — zero mocks.

---

### Step 4 — `test_mismatch_detector.py`

**Service:** `services.mismatch_detector` — Country-code mismatch detection.

`detect(state)` reads `libpostal_town` (or `town_candidate` as fallback) and
`country_code` from state, then cross-checks whether that town exists in the
stated country. If `exact_match` is already `True`, detection is skipped (the
town was resolved in Step 3). Otherwise, it queries `list_countries_for_city()`
to find all countries containing the town. If the town does **not** exist in the
stated country but exists elsewhere, it flags `mismatch_detected=True` and sets
`suggested_country_code` to the highest-population match.

Uses `setdefault` for output keys, so any mismatch already flagged by Step 1
(libpostal country-name detection) is preserved rather than overwritten.

All tests call the **real** `detect()` against the **real** SQLite database —
zero mocks. Generic parameterized test classes drive multiple samples through
the same assertions.

| Test class | Happy / Negative / Edge | What it checks | Expected behaviour |
|---|---|---|---|
| `TestDetectMismatch` | Happy (×6 samples) | Parameterized: Ko Lanta+US, Bari Sardo+IE, Mumbai+US, Tokyo+US, London+JP, San Francisco+AU — town not in stated country | `mismatch_detected=True`, `suggested_country_code` = expected CC (highest-population country) |
| `TestDetectNoMismatch` | Happy (×5 samples) | Parameterized: Ko Lanta+TH, Bari Sardo+IT, Mumbai+IN, London+GB, Springfield+US — town exists in stated country | `mismatch_detected=False`, `suggested_country_code=None` |
| `TestDetectMultiCountryNoMismatch` | Edge (×3 samples) | Parameterized: Springfield+AU, Berlin+US, Paris+US — town exists in MULTIPLE countries including stated one | `mismatch_detected=False` (town found in stated CC too) |
| `TestDetectExactMatchSkips` | Edge (×2 samples) | Parameterized: exact_match=True with various towns — detection bypassed entirely | `mismatch_detected=False`, `suggested_country_code=None` regardless of candidate |
| `TestDetectNoCandidate` | Negative (×3 samples) | Parameterized: None town, empty town, whitespace-only town | Early return, both output fields default |
| `TestDetectTownNotInDB` | Negative (×1) | Town not found in any country (Xyzzyville) | `mismatch_detected=False`, no crash |
| `TestDetectCandidateFallback` | Edge (×2 samples) | Parameterized: libpostal_town=None, town_candidate provides the name instead | Falls back to `town_candidate`, detects mismatch correctly |
| `TestDetectWhitespace` | Edge (×1) | Town name with leading/trailing spaces | Whitespace stripped by `list_countries_for_city`; correct mismatch detected |
| `TestDetectPreservesState` | Edge (×1) | Pre-existing keys survive after `detect()` | `extra_key`, `row_index` etc. not removed |
| `TestDetectFromRawAddress` | E2E (×8 samples) | Parameterized: raw address → `parse()` → `lookup()` → `match()` → `detect()` full pipeline flow | Mismatch for wrong-country addresses (Ko Lanta+US, Bari Sardo+IE, Mumbai+US); no mismatch for correct-country (Springfield+US, Berlin+DE, Plot 16-B Taxila+PK via postal fallback); Step 1 mismatch preserved (Tokyo+JP/US); no match at all (short Taxila+PK address) |

**33 tests** (11 happy-path parameterized + 4 negative + 9 edge-case +
8 end-to-end parameterized), all hitting the real GeoNames SQLite database
and real libpostal parser — zero mocks.

---

### Step 5 — `test_address_scanner.py`

**Service:** `services.address_scanner` — Raw address token scanning fallback.

When earlier steps fail to resolve a city, the scanner tokenizes the raw address
into n-grams and matches them against all known city names for the given country.
Two phases: Phase 1 exact n-gram matching (prefers longest match, skips
single-token stopwords, rejects ambiguous short tokens ≤3 chars); Phase 2 fuzzy
matching via `rapidfuzz` (≥4-char n-grams, threshold 92, ambiguity margin 5).

When `mismatch_detected` is set, Step 5 tries `suggested_country_code` FIRST,
then falls back to `country_code` — same multi-country strategy as Step 3.

All tests call the **real** `scan()` / `_fuzzy_scan()` against the **real**
SQLite database — zero mocks.  **33 tests across 9 classes.**

| Test class | What it checks | Count |
|---|---|---|
| `TestScanExactToken` | Addresses where the city name appears as a clean token → exact n-gram match | 6 |
| `TestScanFuzzy` | Addresses where commas/noise prevent exact match but fuzzy resolves the city | 4 |
| `TestScanNoMatch` | Wrong country, city not in DB, unrecognisable text → scan_match=False | 3 |
| `TestScanEmptyInput` | Empty or whitespace-only address / country code | 3 |
| `TestScanStopwordSkipping` | Stopwords ("de", "la", "du", "road", "st") present but NOT matched as cities | 2 |
| `TestFuzzyScanDirect` | Direct `_fuzzy_scan()` with real city name sets: misspellings, short n-grams, no-match | 6 |
| `TestScanAmbiguousShort` | Very short tokens (≤3 chars) with similar alternatives → ambiguity skip | 1 |
| `TestScanPreservesState` | Running scan() does not clobber pre-existing state fields | 1 |
| `TestScanFromRawAddress` | E2E: raw address → parse → lookup → match → detect → scan (Steps 1–5) | 7 |

---

### Step 6 — `test_llm_parser.py`

**Service:** `address_pipeline_agent.sub_agents.llm_parser.agent` — LLM-based town resolution.

Step 6 resolves cities that earlier deterministic steps could not match. The
agent calls the LLM via LiteLLM with multi-turn tool use against the real
GeoNames database. Input state arrives from Step 5 (address scanner); the agent
builds a system instruction from state context, invokes the LLM, handles tool
calls (native or text-emitted), parses the response (clean JSON, fenced, or
embedded in prose), validates it via `LlmAddressOutput` schema, and writes
`llm_result`, `llm_calls`, token counts, and optionally
`suggested_country_code` to state.

The test file contains two tiers:

1. **Deterministic component tests** — Pure functions (`_parse_llm_text`,
   `_detect_text_tool_call`), LLM tool dispatch (`_execute_tool_call` against
   real GeoNames DB), prompt construction (`build_instruction`), and Pydantic
   schema validation (`LlmAddressOutput`). These run without mocks and complete
   in milliseconds.

2. **Real LLM agent tests** — The full `LlmAddressParserAgent` with a running
   Ollama instance. Auto-skipped when Ollama is not available. These assert on
   **structure** (result keys, call counts, token consumption) rather than exact
   town values because LLM output is non-deterministic. Includes E2E pipeline
   samples (Steps 1–6).

All deterministic tests call the real service functions against the real SQLite
database — zero mocks. Generic parameterized test classes drive multiple samples
through the same assertions.

| Test class | Happy / Negative / Edge | What it checks | Expected behaviour |
|---|---|---|---|
| `TestParseLlmText` | Happy (×3) + Negative (×3) | Parameterized: clean JSON, fenced JSON, embedded JSON in prose, empty string, non-JSON prose, nested fences | Happy: extracts `dict` with correct keys; Negative: returns `None` |
| `TestDetectTextToolCall` | Happy (×1) + Negative (×4) | Parameterized: valid tool call (query_city), final answer (not a tool), unknown tool name, missing arguments key, non-dict input | Happy: returns `(name, args)` tuple; Negative: returns `None` |
| `TestExecuteToolCall` | Happy (×5) + Negative (×2) | Parameterized: `query_city` (known city), `list_countries_for_city` (multi-country), `query_postal_code` (known postal), `query_city_fuzzy` (misspelled), `query_city_by_admin1` (admin1-filtered); Negative: unknown tool, bad arguments | Happy: JSON result with `"result"` key; Negative: JSON result with `"error"` key |
| `TestBuildInstruction` | Happy (×1) + Edge (×2) | Full state injection, sparse state (missing keys default), mismatch flag + suggested_country propagation | Instruction string contains injected values; no KeyError on missing fields |
| `TestLlmAddressOutput` | Happy (×1) + Edge (×5) | Parameterized: valid output, confidence > 1 (percentage→decimal), negative confidence (→0.0), None town (→""), non-numeric confidence (→0.0), suggested_country_code propagation | Lenient validators clamp/coerce values correctly |
| `TestRealLlmHappyPath` | Happy (×3) | Parameterized: known city with postal code, mismatch scenario (town in wrong country), city with disambiguation signal | `llm_result` is not None, has `town` key, `confidence` > 0, `llm_calls` ≥ 1, prompt tokens > 0 |
| `TestRealLlmNegative` | Negative (×2) | Parameterized: gibberish address (no real city), empty address fields | `llm_result` either None or has low confidence; agent does not crash; `llm_calls` ≥ 0 |
| `TestRealLlmEdgeCases` | Edge (×2) | Parameterized: town_candidate key remapping (LLM returns `town_candidate` instead of `town`), suggested_country_code propagation when mismatch detected | Structural validation of state keys; `llm_calls` ≥ 1 |
| `TestLlmFromRawAddress` | E2E (×4) | Parameterized: raw address → parse → lookup → match → detect → scan → llm_parser (Steps 1–6); Italy with postal, Thailand with correct CC, Pakistan (city not in DB), ambiguous multi-country | `llm_result` is not None, has valid structure; pipeline state accumulates all intermediate fields; `llm_calls` ≥ 1 |

**18 deterministic tests** (10 happy-path + 5 negative + 3 edge-case) run
against the real GeoNames SQLite database — zero mocks. **11 real-LLM tests**
(3 happy-path + 2 negative + 2 edge-case + 4 E2E) require a running Ollama
instance and are auto-skipped otherwise. **29 total tests.**

---

### Step 7 — `test_geonames_revalidation.py`

**Service:** `services.geonames_revalidation` — Safety-net re-validation.

Step 7 runs **only on the LLM path** (after Step 6).  Deterministic rows
resolved at Step 3 or Step 5 skip directly to Step 8 — they already resolve
against the GeoNames database, so re-checking is redundant and can regress
correct results (e.g. postal candidate Ko Lanta overriding the correct
libpostal-resolved Krabi).

For LLM rows, Step 7 verifies the LLM's proposed town against
GeoNames through a cascading strategy:

1. Exact match in stated country
2. Exact match in suggested country (if mismatch detected)
3. Fuzzy match (RapidFuzz, threshold 92, margin 5)
4. Cross-country fallback (`list_countries_for_city`)
5. Postal-code table fallback (`search_postal_by_place_name`)
6. Unverified / needs_review if nothing matches

All tests call real service functions against the real GeoNames SQLite DB.
**Zero mocks.**  Sample data is parameterized at the top of the file.
LLM-dependent tests (Flow 8.3) are auto-skipped when Ollama is unavailable.

#### Confidence & Status Outcomes (LLM path only)

| Scenario | Confidence | Status |
|---|---|---|
| No LLM result / not a dict | 0.0 | `needs_review` |
| Empty town from LLM | `CONFIDENCE_LLM_UNVERIFIED` (0.40) | `needs_review` |
| Exact match (stated or suggested CC) | `CONFIDENCE_LLM_CONFIRMED` (0.75) | `validated` |
| Fuzzy match | `CONFIDENCE_LLM_FUZZY_CONFIRMED` (0.70) | `validated` |
| Cross-country fallback | `CONFIDENCE_LLM_CONFIRMED` (0.75) | `validated` |
| Postal-code fallback | `CONFIDENCE_LLM_FUZZY_CONFIRMED` (0.70) | `validated` |
| No match anywhere | `CONFIDENCE_LLM_UNVERIFIED` (0.40) | `needs_review` |

#### Test Classes

| Test class | What it checks | Tier |
|---|---|---|
| `TestDeterministicPassthrough` | Resolved rows pass through with `match_confidence` preserved; various confidence values (1.0, 0.0, fractional) | Deterministic |
| `TestLlmNoResult` | `llm_result` is None, not a dict, or missing `town` → `needs_review` | Deterministic |
| `TestExactMatchStatedCountry` | LLM town found in stated country → `CONFIDENCE_LLM_CONFIRMED`, `validated` | Deterministic |
| `TestExactMatchSuggestedCountry` | LLM town not in stated CC but found in suggested CC → mismatch flagged | Deterministic |
| `TestFuzzyMatch` | LLM town approximately matches a GeoNames name → `CONFIDENCE_LLM_FUZZY_CONFIRMED`, `validated` | Deterministic |
| `TestCrossCountryFallback` | LLM town not in stated CC, no suggested CC, but found via `list_countries_for_city` | Deterministic |
| `TestPostalFallback` | LLM town not in cities1000 but found in postal-codes table | Deterministic |
| `TestNoMatchAnywhere` | LLM town cannot be verified in any table → `needs_review`, warning appended | Deterministic |
| `TestPreferAddressSpelling` | When address has alternate spelling mapping to same geonameid, prefer it | Deterministic |
| `TestFuzzyRevalidate` | Direct tests of `_fuzzy_revalidate` helper: match, ambiguity rejection, short input | Deterministic |
| `TestFlowSteps0_6_7` | **Flow 3:** raw address → Steps 0–6 → revalidate. LLM-resolved rows get re-verified against GeoNames. Requires Ollama — auto-skipped otherwise | Real LLM |

Note: `TestFlowSteps0_3_7` and `TestFlowSteps0_5_7` were removed — Step 7 no
longer runs on deterministic paths (Step 3/5 already resolve against GeoNames).

#### Happy-Path Scenarios

1. **Deterministic resolved row** — `status="resolved"`, `match_confidence=1.0` → passes through unchanged.
2. **Exact match in stated country** — LLM returns a known city (e.g. "Morvi" in IN) → `CONFIDENCE_LLM_CONFIRMED`, `validated`.
3. **Exact match in suggested country** — LLM returns city not in stated CC but in suggested CC → `validated`, `mismatch_detected=True`.
4. **Fuzzy match** — LLM returns a slightly misspelled city name → fuzzy-confirmed.
5. **Cross-country fallback** — Town found globally via `list_countries_for_city` → `validated`, `mismatch_detected=True`.
6. **Postal fallback** — Small town found in postal-codes table → `CONFIDENCE_LLM_FUZZY_CONFIRMED`.
7. **Address spelling preferred** — Raw address has alternate name for same geonameid → preferred over LLM name.
8. **Flow 3 (Steps 0→6→7)** — Address unresolved after Steps 0–5, LLM resolves it, Step 7 re-validates.

Note: Flows 1 (Steps 0→3→8) and 2 (Steps 0→5→8) skip Step 7 entirely —
deterministic paths resolve against GeoNames directly and need no LLM re-validation.

#### Negative Scenarios

1. **No LLM result** — `llm_result=None` → confidence 0.0, `needs_review`.
2. **LLM result not a dict** — `llm_result="some string"` → confidence 0.0, `needs_review`.
3. **Empty town from LLM** — `llm_result={"town": ""}` → `CONFIDENCE_LLM_UNVERIFIED`, `needs_review`.
4. **Unverifiable town** — LLM returns a town not found in any GeoNames table → `needs_review`, `geonames_no_match` warning.
5. **Gibberish address through full pipeline** — E2E flow produces `needs_review` or `validated` (non-deterministic for LLM path).

#### Edge Cases

1. **Zero match_confidence on resolved row** — `status="resolved"`, `match_confidence=0.0` → passes through with 0.0.
2. **Missing keys in state** — Sparse state with only required fields → no crash.
3. **Fuzzy ambiguity rejection** — Multiple close fuzzy matches → disambiguation via raw address tokens.
4. **Short town names** — Names < 3 chars are skipped by fuzzy matching.

---

### Step 8 — `test_persistence.py`

**Service:** `services.persistence` — Final result assembly.

The last pipeline step: assembles the final output dict from the accumulated
state, maps internal statuses to output statuses, rounds confidence, joins
warnings, and computes review reasons. ASCII-normalizes `town` and
`normalized_town` fields via `to_ascii()` (e.g. Brasília→Brasilia,
Medellín→Medellin, Jonquières→Jonquieres).

| Test class | What it checks |
|---|---|
| `TestPersist` | Validated/resolved/needs_review/rejected status mapping, LLM-usage zeros, warning joining, geonames_match flag, mismatch info, confidence rounding, ASCII normalization of town fields |
| `TestComputeReviewReason` | Validated → None, no address data, LLM result present, no LLM result, country_only_address warning → "country_only_address", other status |

---

### Flow 1 E2E — `test_flow_1_e2e.py`

**Pipeline path:** Steps 0→1→2→3→8 (deterministic exact-match resolution).

End-to-end flow tests that run real addresses through the deterministic
pipeline path where Step 3 (exact match) resolves the city. Skips Step 7
(LLM revalidation) — deterministic paths go straight to persist. All tests
use the real GeoNames SQLite database — zero mocks.

| Test class | What it checks |
|---|---|
| `TestFlow1Resolved` | Happy path: address resolves at Step 3 → `persist` produces `validated`, correct town, geonames_id, confidence ≥ 0.95, zero LLM usage |
| `TestFlow1Unresolved` | Negative: Step 3 cannot match → `persist` produces `rejected`, confidence 0.0, no geonames match |
| `TestFlow1EmptyAddress` | Negative: blank or whitespace-only address fields → `rejected` with `no_address_data` warning |
| `TestFlow1FinalResultKeys` | Structural: every key in `final_result` dict is present and typed correctly |

#### Happy-Path Scenarios

1. **Primary exact match** — libpostal extracts town, postal code assists lookup, Step 3 finds a primary-name match → `validated`, `confidence = 1.0`, `geonames_match = True`, `parser_source = "libpostal"`.
2. **Postal-assisted disambiguation** — ambiguous city name (e.g. Springfield), postal code provides admin1 signal → correct geonames_id selected.
3. **Multi-field address** — address spread across `input_address_1`, `input_address_2`, `input_address_3` → all original fields preserved in `final_result`.

#### Negative Scenarios

1. **City not in GeoNames** — real city absent from the GeoNames cities1000 table and postal-codes table, or address too short for libpostal to extract a city candidate → `rejected`, `confidence_score = 0.0`, `geonames_match = False`.
2. **Unparseable address** — gibberish or unconventional format → no match at Step 3, `rejected`.
3. **Empty address fields** — all three address fields blank → `rejected`, `review_reason = "no_address_data"`.

#### Edge Cases

1. **Alternate-name match** — Step 3 matches via an alternate spelling → `validated` with `confidence = 0.95` (not 1.0).
2. **Mismatch CC resolved via suggested country** — address says Thailand but CC=US → Step 3 tries suggested CC (TH) first, resolves Krabi correctly → `validated`, `confidence = 1.0`, `mismatch_detected = True`.
3. **London with wrong CC resolved via suggested country** — London address with CC=AU → Step 3 tries suggested CC (GB) first, resolves London correctly → `validated`, `confidence = 1.0`, `mismatch_detected = True`.
4. **Postal-code fallback for small towns** — City absent from cities500 but present in postal-codes (e.g. Taxila/PK with postcodes 47050/47070/47080) → Step 3's postal-code fallback resolves it → `validated`, `confidence = 0.95`, `match_type = "postal"`.

---

### Flow 2 E2E — `test_flow_2_e2e.py`

**Pipeline path:** Steps 0→1→2→3→4→5→8 (deterministic scanner resolution).

End-to-end flow tests for addresses that Step 3 cannot resolve but the
address scanner (Step 5) catches via n-gram or fuzzy matching. Skips Step 7.
All tests use the real GeoNames SQLite database — zero mocks.

| Test class | What it checks |
|---|---|
| `TestFlow2Resolved` | Happy path: Step 3 fails, Step 5 resolves → `persist` produces `validated`, correct town, `confidence = 0.80`, `parser_source = "geonames_scan"` |
| `TestFlow2Unresolved` | Negative: neither Step 3 nor Step 5 resolves → `persist` produces `rejected`, confidence 0.0 |
| `TestFlow2EmptyAddress` | Negative: blank address → `rejected` with `no_address_data` |
| `TestFlow2FinalResultKeys` | Structural: every key in `final_result` dict is present and typed correctly |

#### Happy-Path Scenarios

1. **Scanner exact-token match** — libpostal misses the city (unusual format), scanner finds it as an exact n-gram → `validated`, `confidence = 0.80`, `geonames_match = True`.
2. **Scanner fuzzy match** — city name has noise or comma-attachment, scanner resolves via fuzzy matching → `validated`, `confidence = 0.80`.
3. **Mismatch detection passthrough** — Step 4 flags a country mismatch, scanner still resolves the city → `mismatch_detected = True` in final result.

#### Negative Scenarios

1. **City not in any table** — completely unrecognisable location → `rejected`, `confidence_score = 0.0`.
2. **City absent from GeoNames** — real city not in cities1000 or postal-codes (e.g. very small villages) → neither Step 3 nor scanner matches, `rejected`.
3. **Empty address fields** — blank input → `rejected`, `review_reason = "no_address_data"`.

Note: Many previously-unresolved wrong-CC scenarios (e.g. London+AU) now resolve
at Step 3 via suggested-CC-first multi-country fallback and no longer reach Flow 2.

#### Edge Cases

1. **Scanner false positive** — road name or region token accidentally matches a city name → `validated` with wrong town (pipeline limitation, documented behaviour).
2. **Step 3 resolves unexpectedly** — if the address happens to resolve at Step 3, the helper exits early with Flow 1 behaviour (`parser_source = "libpostal"`, `confidence ≥ 0.95`).

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
  pytest tests/test_services/test_libpostal_parser.py -v -s 2>&1 \
  | tee tests/test_results/test_libpostal_parser_${TIMESTAMP}.txt

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
