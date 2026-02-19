#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# run_batch.sh — Run the address pipeline batch job
#
# Usage:
#   # 13-row test file with defaults (concurrency=4, batch_size=200)
#   ./scripts/run_batch.sh
#
#   # 13-row test file with explicit options
#   ./scripts/run_batch.sh data/input/test_addresses.xlsx -c 4 -b 5 --loglevel INFO
#
#   # Custom input file
#   ./scripts/run_batch.sh data/input/addresses_32k.csv
#
#   # Custom input + output + concurrency + batch size
#   ./scripts/run_batch.sh data/input/addresses_32k.csv \
#       -o data/output/addresses_32k_output.csv -c 8 -b 500
#
#   # Resume after crash (output path must match the original run)
#   ./scripts/run_batch.sh data/input/addresses_32k.csv \
#       -o data/output/addresses_32k_output.csv -c 8 -b 500 --resume
#
#   # Debug logging
#   ./scripts/run_batch.sh --loglevel DEBUG
#
# Options:
#   <input_file>              Input file (Excel or CSV). Default: data/input/test_addresses.xlsx
#   -o, --output <path>       Output CSV path. Default: data/output/<input>_output.csv
#   -c, --concurrency <n>     Max rows processed concurrently (default: 4)
#   -b, --batch-size <n>      Rows per batch / checkpoint interval (default: 200)
#   --loglevel <LEVEL>        DEBUG | INFO | WARNING | ERROR (default: INFO)
#   --resume                  Resume from last checkpoint (.ckpt.csv next to output)
#
# Environment variables (optional):
#   LLM_CONCURRENCY=4        — parallel LLM calls (match OLLAMA_NUM_PARALLEL)
#   OLLAMA_BASE_URL=http://localhost:11434
# ──────────────────────────────────────────────────────────────
set -euo pipefail

# ── Resolve project root (one level up from scripts/) ─────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# ── Activate virtual environment ──────────────────────────────
if [[ -d "$PROJECT_ROOT/.venv" ]]; then
    source "$PROJECT_ROOT/.venv/bin/activate"
else
    echo "❌ Virtual environment not found at $PROJECT_ROOT/.venv"
    echo "   Run: python -m venv .venv && pip install -r requirements.txt"
    exit 1
fi

# ── Parse arguments ───────────────────────────────────────────
# Detect if --resume is in the args (pass everything through to Python)
PASSTHROUGH_ARGS=()
INPUT_FILE=""
CONCURRENCY=""
LOGLEVEL="INFO"
RESUME=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --resume)
            RESUME=true
            PASSTHROUGH_ARGS+=("--resume")
            shift
            ;;
        -o|--output)
            PASSTHROUGH_ARGS+=("-o" "$2")
            shift 2
            ;;
        -c|--concurrency)
            CONCURRENCY="$2"
            shift 2
            ;;
        --loglevel)
            LOGLEVEL="$2"
            shift 2
            ;;
        -b|--batch-size)
            PASSTHROUGH_ARGS+=("-b" "$2")
            shift 2
            ;;
        -*)
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
        *)
            if [[ -z "$INPUT_FILE" ]]; then
                INPUT_FILE="$1"
            elif [[ -z "$CONCURRENCY" ]]; then
                CONCURRENCY="$1"
            fi
            shift
            ;;
    esac
done

INPUT_FILE="${INPUT_FILE:-data/input/test_addresses.xlsx}"
CONCURRENCY="${CONCURRENCY:-4}"

# ── Verify input file exists ─────────────────────────────────
if [[ ! -f "$INPUT_FILE" ]]; then
    echo "❌ Input file not found: $INPUT_FILE"
    exit 1
fi

# ── Check Ollama is running ──────────────────────────────────
OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
if ! curl -sf "$OLLAMA_URL/api/tags" > /dev/null 2>&1; then
    echo "⚠️  Ollama not reachable at $OLLAMA_URL"
    echo "   Start it with: ollama serve"
    echo "   Continuing anyway (deterministic rows will still work)..."
fi

# ── Run ──────────────────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  📂 Input:       $INPUT_FILE"
echo "  ⚡ Concurrency: $CONCURRENCY"
echo "  📋 Log level:   $LOGLEVEL"
echo "  🤖 LLM conc.:  ${LLM_CONCURRENCY:-1}"
if $RESUME; then
    echo "  🔄 Resuming from checkpoint"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

python -m src.batch_runner "$INPUT_FILE" \
    --concurrency "$CONCURRENCY" \
    --loglevel "$LOGLEVEL" \
    "${PASSTHROUGH_ARGS[@]+${PASSTHROUGH_ARGS[@]}}"
