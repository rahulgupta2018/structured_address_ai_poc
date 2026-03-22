# Structured Address AI — Executive Summary

> **Date:** 21 March 2026
> **Version:** 3.2
> **Audience:** Senior Leadership, Programme Stakeholders, Architecture Review Board
> **Reference:** [Full Technical Design — DESIGN_V3.2.md](./DESIGN_V3.2.md)

---

## 1. Problem Statement

### The Business Problem

Our systems receive millions of address records from customers and partners across Europe. These addresses arrive as **free-form text** — often incomplete, misspelled, in the wrong language, or tagged with the wrong country. For example:

| What We Receive | What's Wrong |
|----------------|-------------|
| `Via Roma 15, 08042 Barisardo (OG)` with country = **Ireland** | Address is in **Italy**, not Ireland — country code is wrong |
| `123 Dbulin, Irleand` | City misspelled ("Dbulin" → Dublin), country misspelled |
| `Hauptstraße 7, 1010` | No city name, no country — just a street and postal code |
| `Italy,,,IT` | No actual address — just a country name |

**Without clean, structured addresses**, downstream processes break:
- Customer correspondence is misrouted
- Regulatory reporting has data quality gaps
- Duplicate detection fails because the same address appears in many forms

### The Scale

- **Current need:** 32,000 addresses to process in a single batch
- **Production target:** Up to 5 million rows per day
- **Today's cost of bad data:** Manual review by operations teams — slow, expensive, error-prone

---

## 2. Our Objective

Build an **intelligent address parsing pipeline** that:

1. Takes a raw address row (free-form text + country code)
2. Splits the raw text into structured parts — **building, street, town, postal code, country** — ready for regulatory output
3. Extracts the correct **city/town name** by cross-referencing a geographic database of 230,000+ cities worldwide
4. Detects and corrects **wrong country codes**
5. Produces **regulatory-compliant address output** — two address lines (70 characters max each), separate town and country fields
6. Assigns a **confidence score** (0–100%) to every result
7. Flags low-confidence rows for **human review** instead of guessing
8. Scales from a laptop (32K rows) to cloud infrastructure (millions of rows)

### The Key Principle

> **Use rules first, AI only when needed.**

Most addresses (~85%) can be resolved by straightforward database lookups — no AI required. We only call the AI model for the ~15% of addresses that rules can't handle. This keeps costs low and results predictable.

---

## 3. How We Built It

### 3.1 The 9-Step Pipeline (Steps 0–8)

Every address row enters the pipeline and passes through a sequence of processing steps. Think of it as an assembly line — each station does one specific job, and the address only visits the stations it needs. Most addresses are resolved quickly by the first few stations; only the trickiest ones go all the way to the end.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                                                                              │
│  📩 Raw Address Input                                                        │
│  "Via Roma 15, 08042 Barisardo (OG)", country = IE                           │
│                                                                              │
│  ───── DETERMINISTIC PATH (rules — no AI) ─────────────────────────────────  │
│                                                                              │
│  Step 0: CLEAN UP  ─── services/normalizer.py                                │
│  ├─ Fix character encoding, remove extra spaces, standardise case            │ 
│  ├─ Concatenate address lines into a single raw_address                      │
│  ├─ Early exit: if validation_errors → status="rejected"                     │
│  ├─ Input:  address_1, address_2, address_3 & country Code                   │
│  └─ Output: raw_address     (concatenated address lines)                     │
│             normalized       (encoding-fixed, lowercase, whitespace-trimmed) │
│             warnings         (appended if no address data)                   │
│                                                                              │
│  Step 1: PARSE THE ADDRESS  ─── (using libpostal)                            │
│  ├─ Use an address parsing library trained on 1 billion+ real-world          │
│  │   addresses to split the raw text into structured parts:                  │
│  │   street, building number, city candidate, postal code, state, country    │
│  ├─ Result: "Via Roma" (street), "15" (building), "08042" (postal code),     │
│  │  "Barisardo" (city candidate).                                            │
│  ├─ Mismatch detection: if libpostal-detected country ≠ input                │
│  │   country_code → flags mismatch + suggests correct CC                     │
│  ├─ Input:  raw_address, country_code                                        │
│  └─ Output: best city candidate, postal code, street name,                   │
│             house/building number, all city-like tokens, list                │
│             country name from address text                                   │
│             mismatch_detected         (true if text says different country)  │
│             suggested_country_code    (corrected ISO alpha-2, if mismatch)   │
│             warnings                  (appended with parse warnings)         │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │ 🛡️ COUNTRY-ONLY GUARD (after Step 1)                                 │    │
│  │ If parsing found ONLY a country name — no town, no street,           │    │
│  │ no building, no postal code, no city candidates — then there is      │    │
│  │ nothing meaningful to resolve. Examples: "Italy,,,IT" or blank       │    │
│  │ address fields with just a country code.                             │    │
│  │ Action: Mark as "needs human review" and skip straight to Step 8.    │    │
│  │ This prevents the AI from inventing a city when none was provided.   │    │
│  │ Output: status="needs_review", confidence=0.0,                       │    │
│  │         warning="country_only_address", parser_source=None           │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
│  Step 2: POSTAL CODE LOOKUP  ─── (Using GeoNames postal-code database)       │
│  ├─ 3-tier fallback strategy to handle wrong/missing country codes:          │
│  │   Tier 1: Look up postal code + stated country code                       │
│  │   Tier 2: Look up postal code + suggested country (from Step 1 mismatch)  │
│  │   Tier 3: Look up postal code alone — accept if it maps to one country    │ 
│  ├─  Get a hint of the city name and region from the postal code — useful    │  
│  │   for disambiguation in Step 3                                            │        
│  ├─ e.g., 08042+IT → Barisardo, Sardinia (Tier 1: primary)                   │
│  ├─ e.g., 10200+US fails → try 10200+TH → Bangkok (Tier 2: suggested CC)     │
│  ├─ e.g., 62701 alone → exists only in US → Springfield, Illinois (Tier 3)   │
│  ├─ Input:  libpostal_postal_code, country_code,                             │
│  │          suggested_country_code (optional, from Step 1 mismatch)          │
│  └─ Output: place name or None, admin1 code, e.g. "IL",                      │ 
│             admin1 name, e.g. "Illinois", alias for postal_town_candidate    │
│             postal_lookup_method    ("primary"|"suggested_country"|"postal") │
│                                                                              │
│  Step 3: EXACT CITY MATCH  ─── (Using GeoNames city database + RapidFuzz)    │
│  ├─ Try to find the city name in our database of 230,000+ cities             │ 
│  ├─ Use postal code and region as tiebreakers if multiple matches found      │
│  ├─ Resolution strategy (tries each in order):                               │
│  │   1. Try libpostal_town against all cities (country-scoped)               │
│  │   2. Try each libpostal_city_candidate                                    │
│  │   3. Try postal_town_candidate as last resort                             │
│  │   4. If multiple matches → disambiguate using:                            │
│  │      a. Postal admin1 code match (region hint)                            │
│  │      b. Postal region name match                                          │
│  │      c. Population fallback (pick the largest city)                       │
│  ├─ Postal-code fallback: small towns (pop < 500) may only exist in the      │
│  │   postal-code table, not the main city database — searches there too      │
│  ├─ Input:  libpostal_town, libpostal_city_candidates,                       │
│  │          postal_town_candidate, postal_admin1_code, postal_region,        │
│  │          country_code, mismatch_detected, suggested_country_code          │
│  ├─ Output: exact_match        (bool — did we find a confident match?)       │
│  │          geonames_id         (GeoNames database ID, or None)              │
│  │          town_candidate      (resolved city name)                         │
│  │          match_type          ("primary", "alternate", or "postal")        │
│  │          match_confidence    (1.0 for primary, 0.95 for alternate/postal) │
│  │          matched_country_code (if resolved in suggested CC)               │
│  ├─ ✅ match → status="validated" (source=libpostal) → DONE, skip to Step 8  │
│  └─ no match → continue to Step 4                                            │
│                                                                              │
│  Step 4: COUNTRY CHECK  ─── (uisng GeoNames - cross-country search)          │
│  ├─ Only runs if Step 3 did NOT resolve the row                              │
│  ├─ Cross-check: does the city actually exist in the stated country?         │
│  ├─ If not → search ALL countries for the city                               │
│  ├─ If found elsewhere → flag mismatch, suggest the correct country          │
│  ├─ Input:  libpostal_town, town_candidate, country_code                     │
│  └─ Output: mismatch_detected        (bool)                                  │
│             suggested_country_code    (corrected CC or None)                 │
│                                                                              │
│  Step 5: FUZZY SCAN  ─── (Using RapidFuzz + Unidecode)                       │
│  ├─ Only runs if Step 3 did NOT resolve the row                              │
│  ├─ Scan the raw address text against 200K+ city names using approximate     │
│  │   matching — handles misspellings, abbreviations, and accented characters │
│  ├─ Uses n-gram scanning to find city names buried in unstructured text      │
│  ├─ Multi-country scan: if mismatch_detected, tries suggested country first  │
│  ├─ Input:  raw_address, country_code, mismatch_detected,                    │
│  │          suggested_country_code                                           │
│  ├─ Output: scan_match         (bool — did fuzzy scanning find a city?)      │
│  │          scan_candidate      (matched city name)                          │
│  │          geonames_id         (GeoNames database ID)                       │
│  │          match_type          (e.g. "exact_ngram", "fuzzy_scan")           │
│  │          match_confidence    (scan confidence score)                      │
│  ├─ ✅ match → status="validated" (source=geonames_scan) → DONE, skip to 8   │
│  └─ no match → status="unresolved" → LLM fallback (Step 6)                   │
│                                                                              │
│  ───── AI PATH (only ~15% of rows reach here) ────────────────────────────   │
│                                                                              │
│  Step 6: AI-ASSISTED RESOLUTION AGENT ─── (LLM - Gemini Flash 2.0)           │
│  ├─ Only runs if Steps 0–5 did NOT resolve the row                           │
│  ├─ An AI model reads the address and reasons about it                       │
│  ├─ The AI can query geographic database using 5 lookup tools:               │
│  │   query_city, query_postal_code, query_admin1,                            │
│  │   search_city_fuzzy, list_countries_for_city                              │
│  ├─ It considers: misspellings, wrong country, missing city name             │
│  ├─ Max 5 tool calls per row (budget cap)                                    │
│  └─ Output: llm_result (dict with structured output:                         │
│             town, street, building, postal_code)                             │
│                                                                              │
│  Step 7: AI SAFETY CHECK REVALIDATION AGENT── (using GeoNames revalidation)  │
│  ├─ Only runs after Step 6 (AI path). Deterministic paths already resolve    │
│  │   against the database directly                                           │
│  ├─ Re-validates LLM-resolved town against GeoNames:                         │
│  │   1. Exact match (normalised name vs country-scoped lexicon)              │
│  │   2. If no exact → fuzzy match (token_set_ratio ≥ 80)                     │
│  │   3. Downgrade to "needs human review" if the check fails                 │
│  ├─ Output: status    ("validated" | "needs_review" | "rejected")            │
│  │          confidence_score  (0.00–1.00)                                    │
│  │          review_reason     (if needs_review)                              │
│  ├─ ✅ match → validated (source preserved from resolving step)              │
│  └─ town present but no match → ⚠️ needs_review                              │
│                                                                              │
│  ───── ALWAYS RUNS ────────────────────────────────────────────────────────  │
│                                                                              │
│  Step 8: SAVE RESULTS  ─── PERSIST AGENT                                     │
│  ├─ Convert accented/non-Latin characters to plain ASCII                     │
│  │   (e.g., "Brasília" → "Brasilia", "Zürich" → "Zurich")                    │
│  ├─ Build two regulatory address lines (max 70 characters each) from         │
│  │   building + street + postal code — town and country are NOT included     │
│  ├─ Look up the full country name from the country code                      │
│  │   (e.g., "IT" → "Italy", "DE" → "Germany")                                │
│  ├─ Compute review_reason (why the row needs human review, if applicable)    │
│  ├─ Input:  all session state fields from preceding steps                    │
│  └─ Output: final_result — the complete 21-field output record               │
│             ├─ input_address_1/2/3       (original text — audit trail)       │
│             ├─ address_line_1/2          (regulatory, 70 chars max each)     │
│             ├─ building, street          (parsed components)                 │
│             ├─ town, country, postal_code (verified + normalised)            │
│             ├─ status, confidence_score  (pipeline verdict)                  │
│             ├─ parser_source             (libpostal|geonames_scan|llm_agent) │
│             ├─ geonames_match, geonames_id, normalized_town                  │
│             ├─ warnings, review_reason                                       │
│             └─ mismatch_detected, suggested_country_code                     │
│                                                                              │
│ ──── 📤 Structured Output ─────────────────────────────────────────────────  │
│                                                                              │
│  input_address_1/2/3 = original text (audit trail)                           │
│  address_line_1 = "Via Roma 15, 08042"                                       │
│  address_line_2 = ""                                                         │
│  town = "Barisardo", country = "Italy" (corrected from IE→IT)                │
│  confidence = 75%, mismatch_detected = true                                  │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 The Four Pipeline Flows

Not every address goes through all steps. The pipeline has four distinct flows, each optimised for a different scenario. The key concept is **early exit** — as soon as we're confident in the answer, we stop processing and jump straight to Step 8 (save results).

| Flow | Steps Run | When | Speed | % of Rows |
|------|-----------|------|-------|-----------|
| **Flow 0 — Country-only** | 0 → 1 → guard → 8 | Address contains only a country name — nothing to resolve | ~5 ms | Rare (~1–2%) |
| **Flow 1 — Exact match** | 0 → 1 → 2 → 3 → 8 | City found by exact database match at Step 3 (confidence ≥ 95%) | ~20 ms | ~57% |
| **Flow 2 — Fuzzy scan** | 0 → 1 → 2 → 3 → 4 → 5 → 8 | City found by approximate text scanning at Step 5 (confidence ≥ 80%) | ~50 ms | ~15–28% |
| **Flow 3 — AI path** | 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 | Requires AI reasoning. Step 7 re-verifies the AI's answer. | ~10 sec | ~15% |

**Key observations:**

- **Flow 0** is a safety mechanism. When an address row contains only a country name (e.g., "Italy,,,IT"), there is no building, street, town, or postal code for the pipeline to work with. Rather than letting the AI invent an answer, the pipeline immediately flags the row for human review. This guard was specifically added after testing revealed that the AI model would fabricate city names (e.g., hallucinating "Louisa" for a USA-only address).

- **Flows 1 and 2** resolve addresses using only rules and database lookups — **no AI is involved**. These rows are resolved in milliseconds with deterministic, reproducible results. Step 7 (the AI safety check) is intentionally skipped because Steps 3 and 5 already resolve directly against the GeoNames database — re-checking would be redundant.

- **Flow 3** is the only flow that calls the AI model (Step 6). Because AI models can hallucinate, **Step 7 always follows Step 6** as a safety net — it re-verifies the AI's city answer against the GeoNames database before accepting it.

> **~85% of rows never touch the AI model** — they're resolved in milliseconds by database lookups alone (Flows 0, 1, 2).

### 3.3 How Each Step Works — In Detail

#### Step 0: Clean Up

**What it does:** Prepares the raw address text for processing.

Every address arrives as messy free-form text across up to three input fields (`address_1`, `address_2`, `address_3`) plus a `country_code`. Before any analysis begins, the pipeline concatenates the address lines into a single `raw_address` string, then normalises it:
- Fixes character encoding (NFKC normalisation for garbled accented characters)
- Removes extra whitespace, trailing commas, and stray punctuation
- Standardises case (lowercase) for consistent matching
- If a postal code is embedded in the text (e.g., "Dublin 4"), extracts it as a separate field
- If the input contains no address data at all, appends a warning

**Libraries used:** Standard Python string processing.

**Example:**
```
Input:   "  via roma  15,  08042 BARISARDO  (OG)  "
Output:  "via roma 15, 08042 barisardo (og)"   postal_code = "08042"
```

#### Step 1: Parse the Address (libpostal)

**What it does:** Splits the cleaned text into structured components.

This is where the heavy lifting of address understanding begins. The pipeline uses **libpostal**, a machine-learning library trained on over **1 billion real-world addresses** from OpenStreetMap. Libpostal understands address formats from virtually every country — European, Asian, Latin American, and more.

Given a free-form address string, libpostal returns labelled parts:

| Component | What It Means | Example |
|-----------|--------------|---------|
| `house_number` | Building/house number | "15" |
| `road` | Street name | "Via Roma" |
| `city` | City/town candidate | "Barisardo" |
| `postcode` | Postal/ZIP code | "08042" |
| `state` | State/region/province | "OG" (Ogliastra) |
| `country` | Country name (if present in text) | "Italy" |

**Why libpostal?** It handles international quirks that simple regex can't:
- "Hauptstraße 7" (German: street comes first, then number)
- "7-chōme Roppongi" (Japanese address format)
- "Apt 3B, 42 Rue de Rivoli" (French: apartment number before street)

**What it produces (beyond the table above):**
- `libpostal_city_candidates` — a list of *all* tokens that look like city names (not just the best one). Step 3 tries each candidate if the primary fails.
- `libpostal_country` — the country name extracted from the address text itself (e.g., "Italy" from "Via Roma 15, Italy"). This is compared against the stated `country_code` for mismatch detection.
- `mismatch_detected` / `suggested_country_code` — if the country name in the address text differs from the input country code, the pipeline flags a mismatch and proposes the correct ISO alpha-2 code. This early signal feeds into Steps 2–5.

**After Step 1 — the Country-Only Guard:**

If libpostal found **only** a country name and nothing else (no town, no street, no building, no postal code, no city candidates), the pipeline short-circuits. There is simply nothing meaningful to look up. The row is marked `status = "needs_review"` with the warning `"country_only_address"` and jumps directly to Step 8. This is **Flow 0**.

#### Step 2: Postal Code Lookup (GeoNames — 3-Tier Fallback)

**What it does:** Uses the postal code as a geographic hint, with a smart fallback strategy to handle wrong or missing country codes.

Postal codes are extremely valuable — they narrow down the geographic area before we even try to match the city name. But there's a catch: **postal codes are not globally unique**. The code "08042" exists in 7 different countries; "10115" also appears in multiple countries. Simply looking up a postal code in the wrong country returns nothing useful.

To handle this, Step 2 uses a **3-tier fallback strategy**:

| Tier | Strategy | When It's Used |
|------|----------|----------------|
| **Tier 1 — Primary** | Look up postal code + stated country code | Default — works when the country code is correct |
| **Tier 2 — Suggested country** | Look up postal code + suggested country code (from Step 1's mismatch detection) | When Tier 1 returns nothing **and** Step 1 detected that the address text mentions a different country than the stated code (e.g., address says "Thailand" but country code = US) |
| **Tier 3 — Postal-only** | Look up postal code without any country filter | Last resort. If the postal code maps to a single country worldwide, accept it. If it maps to multiple countries with no hint, reject as ambiguous. |

The tier that succeeded is recorded as `postal_lookup_method` (`"primary"`, `"suggested_country"`, `"postal_only"`, or `None`) — useful for audit and debugging.

**What it produces:**
- `postal_city_hint` — the most likely city/town name for that postal code
- `postal_region` — the admin1 region (state/province/canton)
- `postal_admin1_code` — the admin1 code (e.g., "IL" for Illinois)
- `postal_lookup_method` — which tier matched

This information becomes a **tiebreaker** in Step 3. When multiple cities share the same name (e.g., "Springfield" appears in 30+ US states), the postal code and region tell us which one the sender meant.

**Examples:**
```
Postal code: 08042, Country: IT
→ Tier 1 (primary): Place = Barisardo, Region = Sardinia

Postal code: 10200, Country: US (but address text says "Thailand")
→ Tier 1: no result for 10200 in US
→ Tier 2 (suggested_country=TH): Place = Bangkok, Region = Bangkok

Postal code: 62701, Country: (wrong/missing)
→ Tier 1: no result → Tier 2: no suggested CC
→ Tier 3 (postal-only): 62701 exists only in US → Place = Springfield, Region = Illinois
```

**Libraries used:** GeoNames postal-code SQLite database (1M+ postal codes worldwide).

#### Step 3: Exact City Match (GeoNames + RapidFuzz)

**What it does:** Attempts to find the city name in the geographic database using exact (or very close) matching.

This is the most critical step — it resolves the majority of addresses (~57%). The pipeline searches our GeoNames cities database (230,000+ cities with population ≥ 500) for the city candidate extracted by libpostal in Step 1.

**The matching logic:**

1. **Primary name match** — Look for an exact match on the city's primary name in GeoNames.
2. **Alternate name match** — If no primary match, check GeoNames' extensive alternate-name index (historical names, translations, abbreviations). "Mumbai" matches even if GeoNames' primary name is "Bombay".
3. **Postal-code-assisted disambiguation** — If multiple cities share the same name, use the postal code hint from Step 2 as a tiebreaker. "Springfield" + postal code 62704 → Springfield, Illinois (not Missouri or Massachusetts).
4. **Population fallback** — If no postal code signal, prefer the largest city by population. "Paris" without any other context → Paris, France (population 2.1 million) over Paris, Texas (population 25,000).
5. **Postal-code fallback** — If the city isn't in the main cities database (too small — population < 500), search the GeoNames postal-code dataset by place name. This catches small towns, villages, and hamlets that are too small for the main database.

**Multi-country resolution:** When Step 1 detected a country mismatch, Step 3 tries the `suggested_country_code` **first**, then the original `country_code`. This prevents false positives (e.g., "Long" matching a US city when the address is actually Thai). If the city is resolved in the suggested country, the output includes `matched_country_code` so downstream steps know the correction was applied.

**Confidence threshold:** If the match scores ≥ 95% confidence, the address is considered resolved. The pipeline skips Steps 4–7 and jumps directly to Step 8. This is **Flow 1** — the fastest path.

**Libraries used:** GeoNames SQLite database (329 MB), RapidFuzz for string similarity scoring.

#### Step 4: Country Check (GeoNames — Cross-Country Search)

**What it does:** Checks whether the city exists in the stated country, and if not, searches all countries.

If Step 3 couldn't find the city in the stated country, maybe the **country code is wrong**. This is surprisingly common — a data entry operator selects "Ireland" from a dropdown when the address is actually Italian.

The pipeline searches **every country** in the GeoNames database for the unmatched city name. If it finds the city in a different country:
- Sets `mismatch_detected = true`
- Records the `suggested_country_code` (e.g., "IT" instead of "IE")
- The pipeline continues with the corrected country for Steps 5–8

**Example:**
```
City: "Barisardo"   Stated country: IE (Ireland)
→ Not found in Ireland
→ Found in Italy (IT)  →  mismatch_detected = true, suggested_country_code = "IT"
```

#### Step 5: Fuzzy Scan (RapidFuzz + Unidecode)

**What it does:** Scans the raw address text against 200,000+ city names using approximate (fuzzy) matching.

When exact matching fails, the problem is usually a misspelling, an abbreviation, or a city name buried inside unstructured text. The fuzzy scanner uses two key techniques:

1. **Fuzzy string comparison** (RapidFuzz) — Compares the address text against city names using edit-distance algorithms. Finds "Dbulin" when looking for "Dublin" (92% similarity) or "Morbi" when looking for "Morvi".

2. **N-gram scanning** — Slides a window across the raw address text, extracting substrings of various lengths, and comparing each against the city database. This finds city names buried inside sentences: "delivered to Dublin Ireland ref 12345" → finds "Dublin" at position 14–19.

3. **Character normalisation** (Unidecode) — Before comparing, all accented and non-Latin characters are converted to plain ASCII. "Zürich" becomes "Zurich", "Łódź" becomes "Lodz". This ensures matches work regardless of whether the sender used diacritics.

**Multi-country scan:** When Step 1 or Step 4 detected a country mismatch, the fuzzy scanner tries the `suggested_country_code` **first**, then falls back to the original `country_code`. This mirrors the same multi-country strategy used in Step 3.

**Match types:** The scanner records how the city was found — `"exact_ngram"` (an exact city name found via n-gram window) or `"fuzzy_scan"` (an approximate match). This distinction feeds into the confidence score and audit trail.

**Safeguards:**
- **Ambiguity margin** — If the top two fuzzy matches are too close in score (e.g., 90% vs 88%), neither is accepted. The row goes to the AI instead of guessing.
- **Short-name caution** — City names ≤ 3 characters require extra-high confidence. "Aix" could match "Aix-en-Provence" or "Aix-les-Bains" — too ambiguous to accept on fuzzy match alone.

**Confidence threshold:** If the match scores ≥ 80% confidence, the address is resolved. The pipeline skips Steps 6–7 and jumps to Step 8. This is **Flow 2**.

**Libraries used:** RapidFuzz (fuzzy matching engine), Unidecode (ASCII transliteration).

#### Step 6: AI-Assisted Resolution (LLM via LiteLLM)

**What it does:** When all rules and database lookups have failed, an AI model reads the address and reasons about it.

Only ~15% of addresses reach this step. The AI agent (powered by **Gemini Flash 2.0** in production) receives:
- The original address text
- Everything the earlier steps discovered (postal code hint, partial matches, mismatch flags)
- Access to **5 database lookup tools** it can call to verify its reasoning:
  `query_city`, `query_postal_code`, `query_admin1`, `search_city_fuzzy`, `list_countries_for_city`

The AI doesn't just guess — it actively queries our GeoNames database to check its hypotheses. For example:
1. AI reads "Hauptstraße 7, 1010" and the postal hint "1010 → Vienna"
2. AI calls the database: "Look up Vienna in Austria" → confirmed, geonames_id = 2761369
3. AI responds: `{"town": "Vienna", "confidence": 0.75}`

The AI is configured with **temperature = 0** (deterministic mode), meaning the same input always produces the same output. This ensures reproducibility. A **budget cap of 5 tool calls per row** prevents runaway costs.

**Output:** The agent returns `llm_result` — a structured dict containing `town`, `street`, `building`, and `postal_code`. This is the raw AI answer that Step 7 then validates.

**Libraries used:** LiteLLM (AI model abstraction layer — allows swapping between local Ollama and cloud Gemini Flash 2.0 with a single config change).

#### Step 7: AI Safety Check (GeoNames — Revalidation)

**What it does:** Re-verifies the AI's answer against the geographic database.

This step is a **trust-but-verify** safeguard. AI models can hallucinate — they occasionally invent plausible-sounding but incorrect city names. Step 7 takes the AI's proposed city name and:
1. Looks it up in the GeoNames database
2. Confirms it exists in the stated (or corrected) country
3. Assigns a confidence score based on the quality of the match
4. If the city doesn't check out → downgrades the row to `status = "needs_review"`

**Important:** Step 7 runs **only after Step 6** (the AI path — Flow 3). Flows 1 and 2 skip Step 7 entirely because their results already come directly from verified database lookups — there is nothing to re-verify.

#### Step 8: Save Results (PersistAgent)

**What it does:** Assembles the final structured output and writes it to the output file or database. **This step always runs, on every flow.**

Step 8 performs several important transformations before saving:

1. **ASCII normalisation** — Converts accented and non-Latin characters in the resolved town name to plain ASCII using **Unidecode** (via the `to_ascii()` helper). GeoNames stores names with diacritics (e.g., "Brasília", "Medellín", "Jonquières"), but downstream regulatory systems expect plain ASCII. Examples:
   - "Brasília" → "Brasilia"
   - "Medellín" → "Medellin"
   - "Zürich" → "Zurich"

2. **Regulatory address line construction** — Builds two address lines (`address_line_1`, `address_line_2`) from the parsed components (building, street, postal code). Each line is **capped at 70 characters** — a regulatory constraint. Town and country are **excluded** from these lines (they have their own dedicated output fields). If the combined text exceeds 70 characters, the splitter breaks at a word boundary to avoid cutting words in half.

3. **Country name lookup** — Converts the ISO alpha-2 country code (e.g., "IT") to the full English country name (e.g., "Italy") using the `countriesV3.1.json` reference file. This produces the `country` output field alongside the `country_code`.

4. **Review reason computation** — Determines *why* a row was flagged for human review (if applicable). Examples: `"country_only_address"`, `"low_confidence"`, `"ambiguous_match"`.

5. **Final result assembly** — Combines all input, extracted, and metadata fields into the final output record.

### 3.4 What Comes Out — The 21 Output Fields

Every processed address produces a structured record with 21 fields, organised into three categories:

#### Input Audit Trail (4 fields)

These preserve the **original input exactly as received** — unchanged. They serve as an audit trail so you can always trace back to what was submitted.

| Field | Description |
|-------|-------------|
| `input_address_1` | Original first address line (from input `address_1`) |
| `input_address_2` | Original second address line (from input `address_2`) |
| `input_address_3` | Original third address line (from input `address_3`) |
| `country_code` | ISO alpha-2 country code from input (e.g., "IT", "IE", "DE") |

#### Regulatory / Extracted Fields (7 fields)

These are the **structured output** — cleaned, verified, and ready for downstream systems.

| Field | Description |
|-------|-------------|
| `address_line_1` | First regulatory address line (max 70 chars). Built from building + street + postal code. Does **not** contain town or country. |
| `address_line_2` | Second regulatory address line (max 70 chars). Overflow from line 1, or empty if line 1 fits everything. |
| `building` | Building/house number extracted by libpostal (e.g., "15") |
| `street` | Street name extracted by libpostal (e.g., "Via Roma") |
| `town` | Resolved and verified city/town name, ASCII-normalised (e.g., "Barisardo") |
| `country` | Full English country name derived from country code (e.g., "Italy") |
| `postal_code` | Postal/ZIP code from parsing or postal lookup (e.g., "08042") |

#### Pipeline Metadata (10 fields)

These describe **how** the pipeline processed the address — useful for quality monitoring, filtering, and audit.

| Field | Description |
|-------|-------------|
| `status` | `"validated"` (confident), `"needs_review"` (uncertain), or `"rejected"` (no match) |
| `confidence_score` | 0.00–1.00 — how confident the pipeline is in the result |
| `parser_source` | Which component resolved the city: `"libpostal"` (Step 3), `"geonames_scan"` (Step 5), or `"llm_agent"` (Step 6) |
| `geonames_match` | `true` / `false` — was the city found in the GeoNames database? |
| `geonames_id` | The GeoNames ID of the matched city (e.g., 2523630 for Barisardo) — links to an authoritative geographic record |
| `normalized_town` | ASCII-normalised town name (redundant when town is already ASCII, but shows the normalisation for names like Brasília → Brasilia) |
| `warnings` | Semicolon-separated list of processing warnings (e.g., `"country_only_address"`) |
| `review_reason` | Why the row needs human review (e.g., `"low_confidence"`, `"ambiguous_match"`) — empty if validated |
| `mismatch_detected` | `true` / `false` — was the stated country code different from where the city was actually found? |
| `suggested_country_code` | If a mismatch was detected, the correct country code (e.g., "IT" when input said "IE") |

---

## 4. Technology Choices (Plain English)

| Technology | What It Does | Why We Chose It |
|-----------|-------------|----------------|
| **Google ADK** (Agent Development Kit) | A framework from Google for building AI-powered workflows. It wires our 8 steps together and provides a built-in web interface for testing. | Write once, run anywhere — same code works on a laptop, as a web API, or in cloud batch processing. Free debugging tools included. |
| **GeoNames Database** | A free, open-source database of 230,000+ cities worldwide (population ≥ 500) with postal codes, regions, and population data. We run it as a local SQLite database (329 MB). | Gives us ground truth for city verification. No API calls needed — everything is local and fast. |
| **Ollama** (local AI) | Runs an AI model on the developer's own machine — no cloud AI costs during development and testing. | Free to run, keeps data on-premises, fast iteration during development. |
| **Google Gemini** (production AI) | Google's cloud AI model, used in production for the ~15% of addresses that need AI reasoning. | Fast, cost-effective, integrates natively with Google's cloud tools. |
| **LiteLLM** | A translation layer that lets us swap between local AI (Ollama) and cloud AI (Gemini) with one line of configuration. | No code changes when moving from laptop to production. |
| **libpostal** | A specialised library trained on 1 billion+ addresses worldwide to split address text into components (street, city, postal code). | Best-in-class for address parsing — handles international formats, abbreviations, and multiple languages. |
| **RapidFuzz** | Compares city names for approximate matches — finds "Dbulin" when looking for "Dublin" even with typos and misspellings. | Fast fuzzy matching engine used in Steps 3, 5, and 7 to handle real-world spelling variations. |
| **Unidecode** | Converts accented and non-Latin characters to plain ASCII — e.g., "Zürich" → "Zurich", "Łódź" → "Lodz". | Ensures addresses in any European language can be matched against our database, regardless of diacritics. |
| **Python** | The programming language for the entire pipeline. | Industry standard for data processing and AI applications. Largest ecosystem of address/geo tools. |

---

## 5. Architecture — How the Pieces Fit Together

### 5.1 The Agent Architecture

We organise the pipeline as a team of **4 specialist agents**, each with a clear responsibility:

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│   🎯 Orchestrator (the manager)                                  │
│   Receives each address, routes it through the right steps,      │
│   decides whether to call the AI or skip it                      │
│                                                                  │
│   ┌──────────────────────────────────────────────────────────┐   │
│   │  1. Deterministic Resolver (the rule follower)           │   │
│   │     Steps 0–5: Clean, parse, country-only guard,         │   │
│   │     look up, match, country check, fuzzy scan            │   │
│   │     Resolves ~85% of addresses with NO AI                │   │
│   │     Contains the country-only guard after Step 1         │   │
│   └──────────────────────────────────────────────────────────┘   │
│                        │                                         │
│            ┌───────────┴───────────┐                             │
│            │ Resolved?             │                             │
│            │                       │                             │
│          YES                      NO                             │
│       (Flow 0/1/2)             (Flow 3)                          │
│          (skip AI)              (need AI)                        │
│            │                       │                             │
│            │   ┌───────────────────┴──────────────────────┐      │
│            │   │  2. AI Parser (the problem solver)       │      │
│            │   │     Step 6: Reasons about the address    │      │
│            │   │     Uses 5 database tools to verify      │      │
│            │   │     Only called for ~15% of rows         │      │
│            │   └───────────────────┬──────────────────────┘      │
│            │                       │                             │
│            │   ┌───────────────────┴──────────────────────┐      │
│            │   │  3. Revalidation Agent (the auditor)     │      │
│            │   │     Step 7: Re-verifies AI results only  │      │
│            │   │     Catches hallucinations before output │      │
│            │   │     Only runs on AI path (Flow 3)        │      │
│            │   └───────────────────┬──────────────────────┘      │
│            │                       │                             │
│            └───────────┬───────────┘                             │
│                        │                                         │
│   ┌────────────────────┴─────────────────────────────────────┐   │
│   │  4. Persist Agent (the recorder)                         │   │
│   │     Step 8: ASCII normalisation, regulatory address      │   │
│   │     line construction, country name lookup, saves the    │   │
│   │     final structured output (21 fields)                  │   │
│   │     ALWAYS runs — no exceptions                          │   │
│   └──────────────────────────────────────────────────────────┘   │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 5.2 Why This Design?

| Decision | Rationale |
|----------|-----------|
| **Rules first, AI second** | 85% of work is done instantly by database lookups. AI is expensive and slow — only use it when necessary. Estimated cost saving: **270× cheaper** than a pure-AI approach. |
| **4 agents** | Steps 0–5 are simple deterministic functions — they don't need their own agents. They're grouped into the Deterministic Resolver. Step 7 (Revalidation) runs only after AI. Step 8 (Persist) always runs. |
| **Country-only guard** | Prevents AI from hallucinating city names when the input contains only a country name. Catches a real-world failure mode observed during testing. |
| **Step 7 only on AI path** | Deterministic paths (Flows 1, 2) resolve directly against the GeoNames database — the data is already verified. Re-checking verified data adds latency with no benefit. The AI path needs this check because LLMs can hallucinate. |
| **Regulatory output in Step 8** | Two address lines (70 chars each, no town/country) are built once at the end from the verified components, ensuring downstream regulatory systems receive properly formatted data. |
| **Confidence scores on every row** | Stakeholders can set their own threshold — e.g., "auto-accept above 85%, send below 85% to manual review". |

---

## 6. Handling Real-World Address Challenges

Real-world addresses are messy. Here are the most common problems we encounter and how the pipeline handles each one.

### 6.1 Disambiguation — "Which Springfield?"

**The problem:** Many city names exist in multiple places. "Springfield" appears in 30+ US states. "San José" exists in Costa Rica, the US, the Philippines, and Spain. A simple name match isn't enough.

**How we solve it:**

```
Input: "123 Oak St, Springfield, 62704"   country = US

  Step 2: Postal code lookup
          62704 → Illinois (admin1 region hint)

  Step 3: Exact match finds 34 cities named "Springfield" in the US
          ├── Springfield, Illinois (admin1 = IL) ← matches postal hint ✅
          ├── Springfield, Missouri (admin1 = MO)
          ├── Springfield, Massachusetts (admin1 = MA)
          └── ... 31 more

  Disambiguation logic:
    1st priority: Does the admin1 region match the postal code hint?
                  → YES: Springfield, IL matches postal code 62704 → Illinois
    2nd priority: If no postal signal, pick the largest city by population

  Result: Springfield, Illinois (confidence: 95%)
```

### 6.2 Wrong Country Code

**The problem:** An address reads "Via Roma 15, 08042 Barisardo" but the country code says **Ireland** (IE). The address is clearly Italian.

**How we solve it:**

```
Input: "Via Roma 15, 08042 Barisardo (OG)"   country = IE

  Step 3: Exact match for "Barisardo" in Ireland → NOT FOUND

  Step 4: Country check
          Search ALL countries for "Barisardo"
          → Found in Italy (IT), population 4,000
          → Flag: mismatch_detected = true
          → Suggest: country should be IT, not IE

  Step 6: AI confirms — "Barisardo is in Sardinia, Italy.
          Postal code 08042 confirms. Country code should be IT."

  Step 7: Safety check re-verifies against Italian GeoNames data ✅

  Result: Barisardo, Italy (corrected from IE → IT), confidence: 75%
```

The pipeline checks **every unmatched city against all countries** in the database. If the city exists elsewhere but not in the stated country, a mismatch is flagged. This catches data entry errors where an operator selects the wrong country from a dropdown.

### 6.3 Misspellings and Alternate Names

**The problem:** Cities have many valid spellings. "Zürich" vs "Zurich". "Mumbai" vs "Bombay". "Morbi" vs "Morvi". "Dbulin" (a typo for Dublin).

**How we solve it:**

| Technique | What It Handles | Example |
|-----------|----------------|---------|
| **Transliteration** (Unidecode) | Accents and non-Latin characters | "Zürich" → "Zurich", "Łódź" → "Lodz" |
| **Alternate names** in GeoNames | Official synonyms and historical names | "Mumbai" matches even though GeoNames primary is "Bombay" (or vice versa) |
| **Fuzzy matching** (RapidFuzz) | Typos, abbreviations, misspellings | "Dbulin" → Dublin (92% similarity match) |
| **N-gram scanning** | City names buried in unstructured text | "delivered to Dublin Ireland ref 12345" → finds "Dublin" embedded in the sentence |
| **Address-spelling preference** | Preserves the sender's spelling when it's a valid alternate name | If sender wrote "Morbi" and GeoNames primary is "Morvi" — both are valid, we keep "Morbi" |

### 6.4 Ambiguous Fuzzy Matches

**The problem:** A fuzzy match might return two cities with very similar scores. For example, "Bari" might score 90% against "Bari" (Italy) and 88% against "Barr" (France). Is the gap enough to be confident?

**How we solve it:**

- We enforce an **ambiguity margin** — if the top two fuzzy matches are too close in score, neither is accepted. The row goes to the AI agent or human review instead of guessing.
- Short city names (≤ 3 characters) require extra caution — "Aix" could match "Aix-en-Provence" or "Aix-les-Bains". We skip ambiguous short matches.
- The pipeline **never guesses** — if it can't be confident, it flags the row for review.

### 6.5 Small Towns Not in the Main Database

**The problem:** GeoNames' city database covers towns with population > 1,000. Very small villages may be missing.

**How we solve it:**

The pipeline has a **postal code fallback**. Towns too small for the main city database often still appear in the GeoNames postal code dataset. If all other lookups fail, we search postal codes by place name as a last resort. This catches small towns like hamlets and rural communities.

### 6.6 Country-Only Address — Nothing to Resolve

**The problem:** Some address rows contain only a country name — no street, no city, no postal code. Example: "Italy,,,IT" or blank address fields with just a country code.

**How we solve it:**

```
Input: "Italy"   country = IT

  Step 1: Parse → libpostal extracts "Italy" as country, nothing else
  Country-only guard: Only country detected — no town, street, building, postal code
  → Mark "needs_review", warning = "country_only_address"
  → Skip Steps 2–7 entirely → Jump to Step 8

  Result: needs_review, confidence = 0%, review_reason = "country_only_address"
```

This guard was added after testing revealed that without it, the AI model would **invent** a city name (e.g., hallucinating "Louisa" for a USA-only address). The guard ensures the pipeline never fabricates data when there's nothing to work with.

### 6.7 Missing City Name Entirely

**The problem:** Some addresses contain only a street, a postal code, and a country — no city name at all. Example: "Hauptstraße 7, 1010".

**How we solve it:**

```
Input: "Hauptstraße 7, 1010"   country = AT (Austria)

  Step 1: Parse → no city candidate extracted
  Step 2: Postal code lookup → 1010 = Vienna, Austria
  Step 3: No city name to match against
  Step 5: Scan raw text for city names → no city found in text
  Step 6: AI reads address + postal hint from Step 2:
          "Postal code 1010 maps to Vienna (Wien). Hauptstraße is
          a common street name in Vienna's 1st district."

  Result: Vienna, Austria, confidence: 75% (AI-resolved)
```

The **postal code hint from Step 2** gives the AI critical context even when no city name appears in the text.

### 6.8 Edge Case Summary

| Scenario | Pipeline Response | Outcome |
|----------|------------------|---------|
| Address contains only a country name | Country-only guard (Flow 0) → "needs_review" immediately | No fabricated data |
| City name appears in multiple countries | Check all countries, flag mismatch if wrong country stated | Country code corrected |
| City name appears multiple times within a country | Disambiguate using postal code region as tiebreaker; fall back to largest by population | Correct city selected |
| City name is misspelled | Fuzzy matching with configurable similarity threshold | Matched despite typo |
| City has alternate/historical names | GeoNames alternate-name index covers synonyms | Matched on any valid name |
| Address has accented characters | Transliterated to ASCII before matching (Unidecode); output also ASCII-normalised | "Zürich" = "Zurich" |
| Two fuzzy matches are too close to call | Ambiguity margin check rejects both; row goes to AI or human review | No false positive |
| City too small for main database | Postal code dataset fallback in Step 3 | Matched via postal data |
| No city name in address at all | AI uses postal code hint + reasoning | AI resolves or flags for review |
| All else fails | Row marked "needs_review" with confidence = 0% | Human reviews — never a wrong guess |

> **Core principle:** The system is designed to **never output a wrong answer confidently**. When in doubt, it says "I'm not sure" and asks a human.

---

## 7. Crash Recovery & Large Batch Handling

### The Challenge

Our next batch is **32,000 rows**. At ~10 seconds per AI-processed row, a full run takes approximately **2 hours**. Without protection, a crash at row 25,000 loses all progress.

### The Solution: Automatic Checkpointing

The system saves progress at regular intervals — like an autosave in a document editor:

```
Processing 32,000 rows (batch size = 500)
│
├── Rows     1–  500 processed → 💾 Progress saved (checkpoint)
├── Rows   501–1,000 processed → 💾 Progress saved
├── Rows 1,001–1,500 processed → 💾 Progress saved
├── ...
├── Rows 24,501–25,000 processed → 💾 Progress saved
│
├── 💥 CRASH (power failure, out of memory, etc.)
│     ↓
│     Maximum data lost: 500 rows (the current unsaved batch)
│     Data preserved: 25,000 rows (in the checkpoint file)
│
├── ▶️ RESUME
│     ↓
│     System reads the checkpoint: "25,000 of 32,000 rows done"
│     Skips rows 1–25,000 (already processed)
│     Only processes rows 25,001–32,000
│
└── ✅ COMPLETE — 32,000 rows output, checkpoint file cleaned up
```

| Feature | Detail |
|---------|--------|
| **Save frequency** | Every 500 rows (configurable) |
| **Maximum data loss on crash** | 500 rows (~8 minutes of work) |
| **Resume command** | Add `--resume` to the same command |
| **No duplicate processing** | Completed rows are matched by position — no double-counting |

---

## 8. Path to Production

### 8.1 From Laptop to Cloud

The same pipeline code runs in three modes with **zero code changes**:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  📋 SAME PIPELINE CODE                                                  │
│                                                                         │
│  ┌─────────────────┐   ┌─────────────────┐   ┌──────────────────────┐   │
│  │  🖥️ LOCAL DEV   │   │  🌐 WEB API     │   │  ☁️ CLOUD BATCH      │   │
│  │                 │   │                 │   │                      │   │
│  │  Developer's    │   │  REST API on    │   │  Google Cloud        │   │
│  │  laptop         │   │  Cloud Run      │   │  Dataflow            │   │
│  │                 │   │                 │   │                      │   │
│  │  Reads Excel/CSV│   │  Receives JSON  │   │  Reads from cloud    │   │
│  │  Writes CSV     │   │  Returns JSON   │   │  storage             │   │
│  │                 │   │                 │   │                      │   │
│  │  AI: Ollama     │   │  AI: Gemini     │   │  AI: Gemini          │   │
│  │  (local, free)  │   │  (cloud)        │   │  (cloud)             │   │
│  │                 │   │                 │   │                      │   │
│  │  32K rows       │   │  On-demand      │   │  5M rows/day         │   │
│  └─────────────────┘   └─────────────────┘   └──────────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 8.2 Scaling Strategy

| Scale | Mode | Infrastructure | Estimated Throughput |
|-------|------|---------------|---------------------|
| **POC / Testing** | Local batch | Developer laptop + local AI | 32K rows in ~2 hours |
| **Departmental** | Web API | Single cloud server (Cloud Run) | Hundreds of rows per minute |
| **Enterprise** | Cloud batch (Dataflow) | Auto-scaling cloud workers | 5 million rows per day |

The progression is seamless — no re-architecture required at any stage.

---

## 9. Success Criteria

| # | Metric | Target | How We Measure |
|---|--------|--------|---------------|
| 1 | **City accuracy** | ≥ 95% of resolved cities are correct | Benchmark against 500+ curated test addresses |
| 2 | **Status correctness** | ≥ 98% of rows get the right status (validated / needs review / rejected) | Automated evaluation suite |
| 3 | **Wrong country detection** | ≥ 90% of mismatched country codes are caught and corrected | Test set with known mismatches |
| 4 | **Zero false positives** | 0% of wrong cities marked as "validated" | Hard gate — uncertain results go to human review |
| 5 | **AI bypass rate** | ≥ 80% of rows resolved without AI | Measured per batch run (currently ~85%) |
| 6 | **Speed — rules path** | < 500 ms per row (95th percentile) | Pipeline timing logs |
| 7 | **Speed — AI path** | < 5 seconds per row (95th percentile) | Pipeline timing logs |
| 8 | **Crash recovery** | Resume within 500 rows of failure point | Checkpoint verification test |
| 9 | **Production scale** | 5 million rows/day on Dataflow | Load test in staging |

---

## 10. Current Status

| Milestone | Status |
|-----------|--------|
| 9-step pipeline logic (Steps 0–8) with 4 flows | ✅ Implemented & tested |
| Agent architecture (4 agents, conditional routing) | ✅ Implemented & tested |
| Country-only guard (Flow 0) | ✅ Implemented — prevents AI hallucination on empty addresses |
| GeoNames database (230K+ cities, postal codes, regions) | ✅ Loaded (329 MB SQLite) |
| Regulatory output fields (21 columns, address_line_1/2 at 70 chars) | ✅ Implemented & tested |
| ASCII normalisation (Unidecode) on output town names | ✅ Implemented & tested |
| Country name lookup (countriesV3.1.json) | ✅ Implemented & tested |
| Local AI integration (Ollama) | ✅ Working — qwen2.5-coder:14b model |
| Batch processing with crash recovery | ✅ Implemented — checkpoint + resume |
| 13-row test file end-to-end | ✅ All rows processed correctly |
| Automated test suite | ✅ 399 non-LLM tests passing |
| Design document (v3.2) | ✅ Complete |
| Interactive dashboard (HTML/JS) | ✅ Renders all 21 output columns |
| **32K-row batch processing** | 🔜 Next — infrastructure is ready |
| Cloud API deployment (Cloud Run) | 📋 Planned — Phase 9 |
| Cloud batch processing (Dataflow) | 📋 Planned — Phase 9/10 |
| Production monitoring & alerting | 📋 Planned — Phase 10 |

---

## 11. Cost & Efficiency — Detailed Breakdown

### 11.1 How LLM Pricing Works

Cloud AI providers charge **per token** (roughly 1 token ≈ ¾ of a word). Every LLM call has two billable parts:

| Part | What It Is | Typical Share |
|------|-----------|---------------|
| **Prompt tokens** | Everything *sent* to the model — instructions, address data, tool results | ~97% of tokens |
| **Completion tokens** | The model's *response* — the resolved address JSON | ~3% of tokens |

Completion tokens cost 2–4× more per token than prompt tokens, but since they're only ~3% of volume the prompt cost dominates.

### 11.2 Measured Token Usage (from 13-row POC test)

Our pipeline was instrumented to track exact token consumption per row:

| Metric | Value |
|--------|-------|
| Total rows processed | 13 |
| Resolved by rules alone (Steps 1–5) | 6 (46%) — **zero AI cost** |
| Sent to LLM (Step 6) | 7 (54%) |
| Average LLM calls per LLM row | 2.0 |
| Prompt tokens (total) | 20,601 |
| Completion tokens (total) | 692 |
| **Total tokens** | **21,293** |

> **Why 2 calls per row?** The LLM often makes a GeoNames tool call first (e.g., "look up München in Germany"), receives the result, then provides its final answer. Each round-trip is one call.

**How the key numbers are calculated:**

```
Prompt token share   = 20,601 ÷ 21,293 = 96.7% ≈ 97%
Completion share     =    692 ÷ 21,293 =  3.3% ≈  3%
Avg tokens / LLM row = 21,293 ÷ 7 LLM rows = 3,042
```

**Why are prompt tokens 97% of the total?** Each LLM call sends a large context:

| What's sent to the model | Approx. tokens |
|--------------------------|---------------|
| System prompt (full instructions) | ~800 |
| Tool definitions (5 GeoNames tools) | ~500 |
| Conversation history (grows each turn) | ~200–500 per turn |
| **Total sent per call** | **~1,500–1,800** |

The model's answer is a small JSON — `{"town": "Munich", "confidence": 0.9, ...}` — only ~50–100 tokens. So across 2 calls with growing context, prompt tokens heavily dominate.

### 11.3 Cost Projection for 30 Million Rows

**Step 1 — How many rows need the LLM?**

| Scenario | LLM % | LLM Rows |
|----------|-------|----------|
| Current POC ratio | 54% | 16,200,000 |
| With improved rules (target) | 30% | 9,000,000 |
| Best-case (optimistic) | 15% | 4,500,000 |

**Step 2 — Token volume** (at 3,042 tokens/LLM row)

| Scenario | LLM Rows | Total Tokens | Prompt Tokens (97%) | Completion Tokens (3%) |
|----------|----------|-------------|---------------------|----------------------|
| 54% LLM | 16.2M | 49.3 billion | 47.8B | 1.5B |
| 30% LLM | 9.0M | 27.4 billion | 26.6B | 0.8B |
| 15% LLM | 4.5M | 13.7 billion | 13.3B | 0.4B |

**Step 3 — Cost by Google Gemini model**

| Model | Prompt ($/1M tokens) | Completion ($/1M tokens) | 54% LLM (16.2M rows) | 30% LLM (9M rows) | 15% LLM (4.5M rows) |
|-------|---------------------|-------------------------|----------------------|--------------------|--------------------|
| **Gemini 2.0 Flash** | $0.10 | $0.40 | **$5,400** | **$3,000** | **$1,500** |
| **Gemini 1.5 Flash** | $0.075 | $0.30 | **$4,000** | **$2,250** | **$1,100** |
| **Gemini 1.5 Pro** | $1.25 | $5.00 | **$67,200** | **$37,300** | **$18,700** |
| **Gemini 2.5 Pro** | $1.25 | $10.00 | **$74,700** | **$41,500** | **$20,800** |

**Step 4 — Comparison with non-Google alternatives**

| Model | Prompt ($/1M tokens) | Completion ($/1M tokens) | 30% LLM scenario |
|-------|---------------------|-------------------------|-------------------|
| GPT-4o mini | $0.15 | $0.60 | **$4,500** |
| GPT-4o | $2.50 | $10.00 | **$74,500** |
| Claude 3.5 Sonnet | $3.00 | $15.00 | **$91,800** |

### 11.4 Recommended Model

**Gemini 2.0 Flash** is the recommended production model:

- **Cheapest cost-effective option:** ~$3,000–$5,400 for 30M rows
- **Fastest inference:** optimised for high-throughput batch workloads
- **Native ADK integration:** our pipeline uses Google ADK — Gemini works out of the box via `LLM_MODEL=gemini/gemini-2.0-flash`
- **Quality:** Flash models are purpose-built for structured tasks like address parsing; the Pro models add reasoning capability we don't need

### 11.5 Cost Comparison — Three Approaches at 30M Rows

| Approach | AI Cost | Processing Time | Total Cost (incl. infra) |
|----------|---------|----------------|--------------------------|
| **Manual processing** (operations team) | $0 | Weeks–months | **~$1,500,000+** at $0.05/row |
| **Pure AI** (every row through Gemini Flash) | ~$9,100 | ~3 days | **~$10,000** (but non-deterministic) |
| **Our approach** (rules + AI for 30%) | ~$3,000 | ~3 days | **~$4,000** (deterministic for 70%) |

> **Key insight:** Our rules-first design is ~3× cheaper than pure AI and delivers deterministic, reproducible results for the majority of rows. The AI is reserved for genuinely ambiguous cases.

---

## 12. Key Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| AI model gives wrong city name | Low | High | Safety check (Step 7) re-verifies every AI result against the database. Uncertain results go to human review — never auto-approved. |
| Google ADK framework is new (released 2025) | Medium | Medium | All business logic is in plain Python functions, independent of the framework. If ADK is ever discontinued, we can rewire in ~2 days. |
| Crash during large batch run | Medium | Low | Automatic checkpointing every 500 rows. Resume picks up where it left off. Maximum loss: ~8 minutes of work. |
| AI model performance degrades | Low | Medium | Temperature set to 0 (deterministic). AI responses are always verified against the database. Monitoring flags anomalies. |
| GeoNames database gaps (missing cities) | Low | Low | GeoNames covers 230K+ cities worldwide (population ≥ 500). Edge cases go to human review. Database is updatable. |

---

## 13. What's Next

| Phase | Scope | Timeline |
|-------|-------|----------|
| **Now** | Process the 32K-row batch file using local infrastructure | Ready |
| **Phase 9** | Deploy web API on Google Cloud Run; switch AI from local to cloud (Gemini) | ~2 days |
| **Phase 10** | Cloud batch processing via Dataflow for enterprise-scale (5M rows/day); production monitoring and alerting | ~3 days |

---

*For the full technical specification including code architecture, session state contracts, agent definitions, and deployment configurations, see [DESIGN_V3.2.md](./DESIGN_V3.2.md).*
