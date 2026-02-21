# Structured Address AI Pipeline

> Intelligent town/city extraction from unstructured, multilingual addresses — powered by [Google ADK](https://google.github.io/adk-docs/) with a deterministic-first, AI-when-needed approach.

The pipeline converts free-text address lines into structured output — extracting and **validating** the town/city against a 200K+ city GeoNames database. ~85% of rows are resolved instantly by rule-based lookups; only the remaining ~15% invoke an LLM.

---

## How It Works

```
Input (Excel / CSV)
  │
  ├─ Step 0  Preprocess — normalize Unicode, whitespace, casing; extract postal code
  ├─ Step 1  libpostal parse — multilingual address parser → town candidate
  ├─ Step 2  Postal code lookup — postal code → region + city hint (disambiguation)
  ├─ Step 3  GeoNames exact match — country-scoped lookup with disambiguation
  │          ✅ If matched → skip Steps 4–6
  ├─ Step 4  Country mismatch detection — cross-validate country code vs address signals
  ├─ Step 5  GeoNames fuzzy scan — n-gram extraction + rapidfuzz scoring
  │          ✅ If matched → skip Step 6
  ├─ Step 6  AI fallback — LLM with 5 GeoNames tools (only ~15% of rows)
  ├─ Step 7  Safety check — re-validate resolved town against GeoNames (always runs)
  └─ Step 8  Persist results
  │
Output (CSV)
```

**Key principle:** No town is ever marked `validated` without a confirmed GeoNames match. When in doubt, the system flags the row for human review rather than guessing.

---

## Architecture

Built on **Google ADK (Agent Development Kit)** — 1 orchestrator + 4 sub-agents:

```
AddressPipelineAgent (orchestrator)
  ├── DeterministicResolverAgent  — Steps 0–5 (rule-based, no AI)
  ├── LlmAddressParserAgent       — Step 6 (AI, skipped if resolved above)
  ├── RevalidationAgent            — Step 7 (safety check, always runs)
  └── PersistAgent                 — Step 8 (write results, always runs)
```

Same code runs in all modes — `adk web` (dev UI), `adk api_server` (REST API), batch CLI, and Dataflow (production).

---

## Quick Start

### Prerequisites

| Requirement | Required? | Purpose |
|-------------|-----------|---------|
| **Python 3.11+** | ✅ Yes | Runtime (3.12 recommended) |
| **GeoNames SQLite DB** | ✅ Yes | City / postal-code verification — `data/database/geonames.db` (~304 MB) |
| **Ollama** + `qwen2.5-coder:14b` | ⚡ Conditional | Local LLM inference (Step 6) — only needed for rows that remain unresolved after Steps 1–5 (~15 % of typical batches) |
| **libpostal** C library | 🔧 Optional | Address parsing (Step 1) — if not installed the parser step is skipped and rows fall through to scan / LLM. Improves accuracy but is **not** required |

### Installation

```bash
git clone https://github.com/rahulgupta2018/structured_address_ai_poc.git
cd structured_address_ai_poc

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
```

### Build the GeoNames Database

Download the three GeoNames source files into `data/reference/`, then run the ETL:

```bash
# 1. Download source files (one-time, ~250 MB total)
cd data/reference
curl -O https://download.geonames.org/export/dump/cities500.zip
curl -O https://download.geonames.org/export/zip/allCountries.zip
curl -O https://download.geonames.org/export/dump/admin1CodesASCII.txt
unzip cities500.zip          # → cities500.txt  (229K cities, pop ≥ 500)
unzip allCountries.zip       # → allCountries.txt
cd ../..

# 2. Build SQLite DB (~2 min, creates data/database/geonames.db)
python -m src.geonames_etl
```

### Optional: libpostal & Ollama

```bash
# macOS — install libpostal (optional, improves Step 1 accuracy)
brew install libpostal
CFLAGS="-I/opt/homebrew/include" LDFLAGS="-L/opt/homebrew/lib" pip install postal

# Pull the LLM model (needed only if rows reach Step 6)
ollama pull qwen2.5-coder:14b
```

### Run the Pipeline

```bash
# 13-row test file with defaults
./scripts/run_batch.sh

# Explicit options
./scripts/run_batch.sh data/input/test_addresses.xlsx -c 4 -b 5 --loglevel INFO

# Large batch (32K rows, recommended settings)
./scripts/run_batch.sh data/input/addresses_32k.csv \
    -o data/output/addresses_32k_output.csv -c 8 -b 500

# Resume after crash
./scripts/run_batch.sh data/input/addresses_32k.csv \
    -o data/output/addresses_32k_output.csv -c 8 -b 500 --resume

# Or call Python directly
python -m src.batch_runner data/input/test_addresses.xlsx -c 4 -b 5

# Run test scripts 
.venv/bin/python -m pytest tests/test_services/test_single_row_cost.py -v -s 2>&1
.venv/bin/python -m pytest tests/test_services/test_single_row_cost.py -v -s -W ignore::DeprecationWarning 2>&1 | tee logs/test_cost.log
```

### ADK Dev UI

```bash
# Launch the interactive browser-based trace viewer
adk web --port 8000
# Opens http://localhost:8000 — select "address_pipeline_agent" from the dropdown
```

### Run Tests

```bash
pytest tests/ -v
```

### Results Dashboard

The project includes an interactive HTML dashboard for analysing pipeline output files (CSV / Excel). No build step required — just serve the `dashboard/` folder.

```bash
# Start the dashboard server
cd dashboard && python3 -m http.server 8765
# Opens at http://localhost:8765
```

```bash
# Restart the server (kill existing, then re-launch)
pkill -f "http.server 8765" 2>/dev/null; sleep 1
cd dashboard && python3 -m http.server 8765
```

```bash
# Stop the server
pkill -f "http.server 8765"
```

**Features:** Upload any output CSV/Excel → 7 KPI cards, 4 interactive charts (status, parser, confidence, country), 8 filter slicers, sortable paginated data table, row detail modal, filtered CSV export.  
**Themes:** Lloyds Bank (default, light) and Dark — switchable from the top bar, persisted in localStorage.

---

## Input Format

Excel (`.xlsx`) or CSV. Flexible column naming:

| Column | Aliases Accepted | Required |
|--------|-----------------|----------|
| `address_1` | `addr_1`, `address_line_1`, `line_1` | nullable |
| `address_2` | `addr_2`, `address_line_2`, `line_2` | nullable |
| `address_3` | `addr_3`, `address_line_3`, `line_3` | nullable |
| `country_code` | `cc`, `country` | **yes** (ISO 3166-1 alpha-2) |

At least one address line and a valid country code are required per row.

## Output Fields

| Field | Description |
|-------|-------------|
| `town` | Extracted and validated town/city name |
| `status` | `validated` · `needs_review` · `rejected` |
| `confidence_score` | 0.00–1.00 composite score |
| `parser_source` | Resolution method: `libpostal` · `geonames_scan` · `llm_agent` |
| `geonames_match` | Whether a GeoNames match was confirmed |
| `geonames_id` | GeoNames ID of the matched city |
| `normalized_town` | Normalised form used for matching |
| `warnings` | Semicolon-separated list of issues encountered |
| `review_reason` | Why a row was flagged for human review |

---

## Confidence Scores

| Tier | Score | Meaning |
|------|-------|---------|
| Exact match (primary) | 1.00 | GeoNames primary name matches exactly |
| Exact match (alternate) | 0.95 | GeoNames alternate name matches |
| Fuzzy scan | 0.80 | High-confidence fuzzy match from raw address |
| LLM + exact re-validation | 0.75 | LLM proposed town confirmed by exact lookup |
| LLM + fuzzy re-validation | 0.70 | LLM proposed town confirmed by fuzzy match |
| LLM unverified | 0.40 | LLM proposed town but no GeoNames match |
| Rejected | 0.00 | No town could be determined |

---

## Configuration

All tuneable parameters live in `utils/config.py` and can be overridden via environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `FUZZY_MATCH_THRESHOLD` | `92` | Minimum fuzzy score to accept (0–100) |
| `FUZZY_AMBIGUITY_MARGIN` | `5` | Min gap between top-2 candidates |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API endpoint |
| `LLM_MODEL` | `ollama_chat/qwen2.5-coder:14b` | LiteLLM model identifier |
| `LLM_TIMEOUT_SECONDS` | `120` | Per-request timeout |
| `LLM_CONCURRENCY` | `1` | Parallel LLM calls (match `OLLAMA_NUM_PARALLEL`) |

---

## Checkpointing & Crash Recovery

For large batches (e.g. 32K rows), the pipeline writes a **rolling checkpoint** after each batch of rows. If the process crashes, resume with `--resume` to skip already-processed rows:

```bash
# Crashes at row 25,000 → checkpoint has 25,000 rows saved
# Resume picks up at row 25,001
python -m src.batch_runner data/input/big.csv -o data/output/big_output.csv --resume
```

| Setting | Default | Recommendation for 32K rows |
|---------|---------|----------------------------|
| `--batch-size` | 200 | 500 (checkpoint every ~10 min) |
| `--concurrency` | 4 | 8 (saturate 4 Ollama slots + 4 deterministic) |

---

## Logging

- **Console** — controlled by `--loglevel` (default: `INFO`)
- **Log files** — every run writes a timestamped log to `logs/batch_<YYYYMMDD_HHMMSS>.log`

```bash
# Trace a specific row
grep -i "row_7\|barisardo" logs/batch_20260219_140000.log
```

---

## Project Structure

```
structured_address_ai_poc/
│
├── address_pipeline_agent/            # ADK agent app (adk web/run discovers this)
│   ├── agent.py                       # root_agent = AddressPipelineAgent(...)
│   └── sub_agents/
│       ├── deterministic_resolver/    # Steps 0–5: rule-based resolution
│       ├── llm_parser/               # Step 6: LLM with GeoNames tools
│       ├── revalidation/             # Step 7: safety-net re-validation
│       └── persist/                   # Step 8: result persistence
│
├── services/                          # Business logic — plain Python, no ADK dependency
│   ├── normalizer.py                  # preprocess() — Step 0
│   ├── libpostal_parser.py            # libpostal_parse() — Step 1
│   ├── postal_lookup.py              # postal_code_lookup() — Step 2
│   ├── geonames_exact.py             # exact_match() — Step 3
│   ├── mismatch_detector.py          # mismatch_detect() — Step 4
│   ├── address_scanner.py            # geonames_scan() — Step 5
│   ├── geonames_revalidation.py      # revalidate() — Step 7
│   ├── geonames_repo.py              # GeoNames DB query layer (shared)
│   ├── io_reader.py                   # Read Excel/CSV input
│   ├── io_writer.py                   # Write CSV/Excel output
│   └── persistence.py                # Cloud SQL + GCS (production)
│
├── utils/                             # Shared config, schemas, prompts
│   ├── config.py                      # Paths, thresholds, LLM settings
│   ├── schemas.py                     # Pydantic models & state key constants
│   └── prompts.py                     # LLM system prompt for Step 6
│
├── src/
│   ├── batch_runner.py                # CLI entry point: python -m src.batch_runner
│   └── geonames_etl.py               # GeoNames data loader → SQLite
│
├── scripts/
│   └── run_batch.sh                   # Shell wrapper with --resume support
│
├── tests/
│   ├── test_agents/                   # Agent-level tests (with ADK Runner)
│   ├── test_services/                 # Service tests — plain pytest, no ADK
│   └── benchmark/                     # Evaluation dataset
│
├── data/
│   ├── database/geonames.db          # SQLite (304 MB) — cities, variants, postal
│   ├── reference/                     # Raw GeoNames TSV files
│   ├── input/                         # Input files
│   └── output/                        # Pipeline output (.gitignored)
│
├── docs/
│   ├── DESIGN_V3.2.md                # Full technical design document
│   └── EXECUTIVE_SUMMARY.md          # Stakeholder-friendly overview
│
├── dashboard/                         # Interactive results dashboard (static HTML)
│   ├── index.html                     # Layout & structure
│   ├── style.css                      # Themed styles (Lloyds Bank / Dark)
│   ├── dashboard.js                   # Charts, filters, table, export logic
│   └── vendor/                        # Bundled libs (zero CDN — works offline)
│       ├── chart.umd.min.js           # Chart.js 4.4.1
│       ├── xlsx.full.min.js           # SheetJS 0.18.5
│       └── fonts/                     # Inter font (woff2 + @font-face CSS)
│
├── .env.example                       # Environment variable template
├── requirements.txt
└── .gitignore
```

---

## Documentation

| Document | Audience | Description |
|----------|----------|-------------|
| [EXECUTIVE_SUMMARY.md](docs/EXECUTIVE_SUMMARY.md) | Senior stakeholders | Plain-English overview: problem, solution, architecture, success criteria |
| [DESIGN_V3.2.md](docs/DESIGN_V3.2.md) | Engineering | Full technical spec: agent definitions, state contracts, deployment, checkpointing |

---

## License

Private — not for distribution.
