# Structured Address AI — Executive Summary

> **Date:** 19 February 2026
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
2. Extracts the correct **city/town name** by cross-referencing a geographic database of 230,000+ cities worldwide
3. Detects and corrects **wrong country codes**
4. Assigns a **confidence score** (0–100%) to every result
5. Flags low-confidence rows for **human review** instead of guessing
6. Scales from a laptop (32K rows) to cloud infrastructure (millions of rows)

### The Key Principle

> **Use rules first, AI only when needed.**

Most addresses (~85%) can be resolved by straightforward database lookups — no AI required. We only call the AI model for the ~15% of addresses that rules can't handle. This keeps costs low and results predictable.

---

## 3. How We Built It

### 3.1 The 8-Step Pipeline

Every address row goes through up to 8 steps. Think of it as an assembly line — each station does one specific job:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  📩 Raw Address Input                                                   │
│  "Via Roma 15, 08042 Barisardo (OG)", country = IE                      │
│                                                                         │
│  ───── FAST PATH (rules — no AI) ─────────────────────────────────────  │
│                                                                         │
│  Step 0: CLEAN UP                                                       │
│  ├─ Fix character encoding, remove extra spaces, standardise case       │
│  └─ Extract postal code if embedded in the text                         │
│                                                                         │
│  Step 1: PARSE THE ADDRESS                                              │
│  ├─ Use an address parsing library to split the text into parts:        │
│  └─ street, building number, city candidate, postal code, state         │
│                                                                         │
│  Step 2: POSTAL CODE LOOKUP                                             │
│  ├─ Look up the postal code in our geographic database                  │
│  └─ Get a hint about which city and region this postal code belongs to  │
│                                                                         │
│  Step 3: EXACT CITY MATCH                                               │
│  ├─ Try to find the city name in our database (exact match)             │
│  ├─ Use postal code and region as tiebreakers if multiple matches       │
│  └─ ✅ If confident match found → DONE (skip Steps 4–6)                │
│                                                                         │
│  Step 4: COUNTRY CHECK                                                  │
│  ├─ Cross-check: does the city actually exist in the stated country?    │
│  └─ If not → flag mismatch, suggest the correct country                 │
│                                                                         │
│  Step 5: FUZZY SCAN                                                     │
│  ├─ Scan the raw address text against 200K+ city names                  │
│  ├─ Handles misspellings and abbreviations                              │
│  └─ ✅ If confident match found → DONE (skip Step 6)                   │
│                                                                         │
│  ───── AI PATH (only ~15% of rows reach here) ───────────────────────  │
│                                                                         │
│  Step 6: AI-ASSISTED RESOLUTION                                         │
│  ├─ An AI model reads the address and reasons about it                  │
│  ├─ The AI can query our geographic database using 5 lookup tools       │
│  ├─ It considers: misspellings, wrong country, missing city name        │
│  └─ Returns a verified city name with reasoning                         │
│                                                                         │
│  ───── ALWAYS RUNS ───────────────────────────────────────────────────  │
│                                                                         │
│  Step 7: SAFETY CHECK                                                   │
│  ├─ Re-verify the resolved city against the geographic database         │
│  ├─ Assign final confidence score                                       │
│  └─ Downgrade to "needs human review" if the check fails               │
│                                                                         │
│  Step 8: SAVE RESULTS                                                   │
│  └─ Write the structured result to the output file / database           │
│                                                                         │
│  📤 Structured Output                                                   │
│  town = "Barisardo", country = IT (corrected), confidence = 75%         │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 3.2 The Three Possible Paths

Not every address goes through all 8 steps:

| Path | Steps Run | When | Speed | % of Rows |
|------|-----------|------|-------|-----------|
| **Fast path** | 0 → 1 → 2 → 3 → 7 → 8 | City found by exact match | ~20 ms | ~70% |
| **Fuzzy path** | 0 → 1 → 2 → 3 → 4 → 5 → 7 → 8 | City found by fuzzy scan | ~50 ms | ~15% |
| **AI path** | 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 | Requires AI reasoning | ~10 sec | ~15% |

> **85% of rows never touch the AI model** — they're resolved in milliseconds by database lookups alone.

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

We organise the pipeline as a team of **5 specialist agents**, each with a clear responsibility:

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│   🎯 Orchestrator (the manager)                                  │
│   Receives each address, routes it through the right steps,      │
│   decides whether to call the AI or skip it                      │
│                                                                  │
│   ┌──────────────────────────────────────────────────────────┐   │
│   │  1. Deterministic Resolver (the rule follower)           │   │
│   │     Steps 0–5: Clean, parse, look up, match             │   │
│   │     Resolves ~85% of addresses with NO AI               │   │
│   └──────────────────────────────────────────────────────────┘   │
│                        │                                         │
│            ┌───────────┴───────────┐                             │
│            │ Resolved?             │                             │
│            │                       │                             │
│          YES                      NO                             │
│          (skip AI)              (need AI)                        │
│            │                       │                             │
│            │   ┌───────────────────┴──────────────────────┐      │
│            │   │  2. AI Parser (the problem solver)       │      │
│            │   │     Step 6: Reasons about the address    │      │
│            │   │     Uses 5 database tools to verify      │      │
│            │   │     Only called for ~15% of rows         │      │
│            │   └───────────────────┬──────────────────────┘      │
│            │                       │                             │
│            └───────────┬───────────┘                             │
│                        │                                         │
│   ┌────────────────────┴─────────────────────────────────────┐   │
│   │  3. Safety Checker (the auditor)                         │   │
│   │     Step 7: Re-verifies the result against the database  │   │
│   │     Assigns confidence score, flags uncertain results    │   │
│   │     ALWAYS runs — no exceptions                          │   │
│   └────────────────────┬─────────────────────────────────────┘   │
│                        │                                         │
│   ┌────────────────────┴─────────────────────────────────────┐   │
│   │  4. Result Writer (the recorder)                         │   │
│   │     Step 8: Saves the final structured address           │   │
│   │     ALWAYS runs — no exceptions                          │   │
│   └──────────────────────────────────────────────────────────┘   │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 5.2 Why This Design?

| Decision | Rationale |
|----------|-----------|
| **Rules first, AI second** | 85% of work is done instantly by database lookups. AI is expensive and slow — only use it when necessary. Estimated cost saving: **270× cheaper** than a pure-AI approach. |
| **5 agents** | Steps 0–5 are simple functions — they don't need their own agents. |
| **Safety check always runs** | Every resolved city is re-verified against the database, regardless of how it was resolved. No unverified result reaches the output. |
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

### 6.6 Missing City Name Entirely

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

### 6.7 Edge Case Summary

| Scenario | Pipeline Response | Outcome |
|----------|------------------|---------|
| City name appears in multiple countries | Check all countries, flag mismatch if wrong country stated | Country code corrected |
| City name appears multiple times within a country | Disambiguate using postal code region as tiebreaker; fall back to largest by population | Correct city selected |
| City name is misspelled | Fuzzy matching with configurable similarity threshold | Matched despite typo |
| City has alternate/historical names | GeoNames alternate-name index covers synonyms | Matched on any valid name |
| Address has accented characters | Transliterated to ASCII before matching | "Zürich" = "Zurich" |
| Two fuzzy matches are too close to call | Ambiguity margin check rejects both; row goes to AI or human review | No false positive |
| City too small for main database | Postal code dataset fallback | Matched via postal data |
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
│  ┌─────────────────┐   ┌─────────────────┐   ┌──────────────────────┐  │
│  │  🖥️ LOCAL DEV    │   │  🌐 WEB API      │   │  ☁️ CLOUD BATCH      │  │
│  │                  │   │                  │   │                      │  │
│  │  Developer's     │   │  REST API on     │   │  Google Cloud        │  │
│  │  laptop          │   │  Cloud Run       │   │  Dataflow            │  │
│  │                  │   │                  │   │                      │  │
│  │  Reads Excel/CSV │   │  Receives JSON   │   │  Reads from cloud    │  │
│  │  Writes CSV      │   │  Returns JSON    │   │  storage             │  │
│  │                  │   │                  │   │                      │  │
│  │  AI: Ollama      │   │  AI: Gemini      │   │  AI: Gemini          │  │
│  │  (local, free)   │   │  (cloud)         │   │  (cloud)             │  │
│  │                  │   │                  │   │                      │  │
│  │  32K rows        │   │  On-demand       │   │  5M rows/day         │  │
│  └─────────────────┘   └─────────────────┘   └──────────────────────┘  │
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
| 8-step pipeline logic (Steps 0–8) | ✅ Implemented & tested |
| Agent architecture (5 agents, conditional routing) | ✅ Implemented & tested |
| GeoNames database (230K+ cities, postal codes, regions) | ✅ Loaded (329 MB SQLite) |
| Local AI integration (Ollama) | ✅ Working — qwen2.5-coder:14b model |
| Batch processing with crash recovery | ✅ Implemented — checkpoint + resume |
| 13-row test file end-to-end | ✅ All rows processed correctly |
| Design document (v3.2) | ✅ Complete |
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
