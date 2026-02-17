# Structured Address AI Pipeline

> ISO 20022-compliant town extraction from unstructured, multilingual addresses.

The pipeline converts free-text address lines into structured output — extracting and **validating** the town/city against a geographic gazetteer (GeoNames). It uses a deterministic-first approach, escalating to an LLM only when rule-based methods fail.

---

## How It Works

```
Excel Input (.xlsx)
  │
  ├─ Step 0  Preprocess — normalize Unicode, whitespace, casing
  ├─ Step 1  libpostal — multilingual address parser → town candidate
  ├─ Step 2  GeoNames exact match — country-scoped lookup (primary / ascii / alternate names)
  ├─ Step 3  GeoNames fuzzy scan — n-gram extraction + rapidfuzz scoring
  ├─ Step 4  LLM fallback — Ollama (temp=0, JSON schema, few-shot)
  ├─ Step 5  Re-validation — exact + fuzzy match of LLM output against GeoNames
  └─ Step 6  Decision engine — status, confidence score, warnings
  │
Excel Output (.xlsx)
```

**Key principle:** No town is ever marked `validated` without a confirmed GeoNames match.

---

## Quick Start

### Prerequisites

- **Python 3.11+**
- **libpostal** C library (optional but recommended):
  ```bash
  brew install libpostal          # macOS
  ```
- **Ollama** running locally (for LLM fallback):
  ```bash
  ollama pull qwen2.5-coder:14b
  ```
- **GeoNames data** — `data/reference/cities5000.txt` is included in the repo.

### Installation

```bash
# Clone the repo
git clone https://github.com/rahulgupta2018/structured_address_ai_poc.git
cd structured_address_ai_poc

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# If libpostal is installed via Homebrew (macOS):
CFLAGS="-I/opt/homebrew/include" LDFLAGS="-L/opt/homebrew/lib" pip install postal
```

### Run the Pipeline

```bash
# Full pipeline (with LLM fallback)
python -m src.main --input data/samples/test_addresses.xlsx

# Deterministic-only mode (no LLM)
python -m src.main --input data/samples/test_addresses.xlsx --skip-llm

# Custom output path
python -m src.main --input data/samples/test_addresses.xlsx --output results.xlsx

# Verbose logging
python -m src.main --input data/samples/test_addresses.xlsx --log-level DEBUG
```

### Run Tests

```bash
pytest tests/ -v
```

---

## Input Format

The input Excel file should contain these columns (flexible naming supported):

| Column           | Aliases accepted                         | Required |
|------------------|------------------------------------------|----------|
| `address_1`      | `addr_1`, `address_line_1`, `line_1`     | nullable |
| `address_2`      | `addr_2`, `address_line_2`, `line_2`     | nullable |
| `address_3`      | `addr_3`, `address_line_3`, `line_3`     | nullable |
| `country_code`   | `cc`, `country`                          | **yes** (ISO 3166-1 alpha-2) |

At least one address line and a valid country code are required per row.

## Output Fields

| Field              | Description                                         |
|--------------------|-----------------------------------------------------|
| `town`             | Extracted and validated town/city name               |
| `status`           | `validated` · `needs_review` · `rejected`            |
| `confidence_score` | 0.0–1.0 composite score                             |
| `parser_source`    | Stage that resolved the town (`libpostal` · `geonames_scan` · `llm`) |
| `geonames_match`   | Whether a GeoNames match was confirmed               |
| `geonames_id`      | GeoNames ID of the matched city                      |
| `warnings`         | Semicolon-separated list of issues encountered       |

Output files are timestamped: `data/output/<input_stem>_output_<YYYYMMDD_HHMMSS>.xlsx`

---

## Confidence Scores

| Tier                     | Score | Meaning                                     |
|--------------------------|-------|---------------------------------------------|
| Exact match (primary)    | 1.00  | GeoNames primary name matches exactly        |
| Exact match (alternate)  | 0.95  | GeoNames alternate name matches              |
| Fuzzy scan               | 0.80  | High-confidence fuzzy match from raw address  |
| LLM + exact re-validation| 0.75  | LLM proposed town confirmed by exact lookup   |
| LLM + fuzzy re-validation| 0.70  | LLM proposed town confirmed by fuzzy match    |
| LLM unverified           | 0.40  | LLM proposed town but no GeoNames match       |
| Rejected                 | 0.00  | No town could be determined                   |

---

## Configuration

All tuneable parameters are in `src/config.py` and can be overridden via environment variables:

| Variable                | Default                  | Description                          |
|-------------------------|--------------------------|--------------------------------------|
| `FUZZY_MATCH_THRESHOLD` | `92`                     | Minimum fuzzy score to accept (0–100)|
| `FUZZY_AMBIGUITY_MARGIN`| `5`                      | Min gap between top-2 candidates     |
| `OLLAMA_BASE_URL`       | `http://localhost:11434` | Ollama API endpoint                  |
| `OLLAMA_MODEL`          | `qwen2.5-coder:14b`     | LLM model name                       |
| `LLM_TIMEOUT_SECONDS`   | `30`                     | Per-request timeout                  |
| `LLM_BATCH_SIZE`        | `10`                     | Rows sent to LLM per batch           |

---

## Logging

- **Console** — controlled by `--log-level` (default: `INFO`)
- **Log files** — every run writes a full `DEBUG` trace to `logs/run_<timestamp>.log`

To trace a specific row after a run:
```bash
grep -i "court\|etienne" logs/run_20260217_120021.log
```

---

## Project Structure

```
structured_address_ai_poc/
├── src/
│   ├── config.py              # Paths, thresholds, LLM settings
│   ├── schemas.py             # Pydantic models (AddressInput, AddressOutput, etc.)
│   ├── preprocess.py          # Unicode normalization, tokenization, n-grams
│   ├── geonames_loader.py     # Load & index cities5000.txt by country
│   ├── geonames_matcher.py    # Exact & fuzzy matching against GeoNames
│   ├── geonames_scan.py       # Raw address n-gram scan
│   ├── parser_libpostal.py    # libpostal integration (graceful fallback)
│   ├── llm_ollama.py          # Ollama LLM fallback with retry & JSON parsing
│   ├── decision_engine.py     # Status & confidence assignment
│   ├── io_excel.py            # Excel read/write with column alias mapping
│   ├── pipeline.py            # Orchestrator — ties all stages together
│   └── main.py                # CLI entry point
├── tests/
│   ├── test_preprocess.py
│   ├── test_geonames_matcher.py
│   ├── test_geonames_scan.py
│   ├── test_decision_engine.py
│   └── test_pipeline_e2e.py
├── data/
│   ├── reference/cities5000.txt   # GeoNames gazetteer (~67K cities)
│   ├── samples/                   # Sample input files
│   └── output/                    # Generated output files (.gitignored)
├── docs/
│   └── DESIGN.md                  # Full architecture & design document
├── logs/                          # Run logs (.gitignored)
├── requirements.txt
└── .gitignore
```

---

## Design Document

See [docs/DESIGN.md](docs/DESIGN.md) for the full architecture, design rationale, anti-hallucination controls, and implementation details.

---

## License

Private — not for distribution.
