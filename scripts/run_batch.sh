#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# run_batch.sh — Run the address pipeline batch job
#
# Usage:
#   ./scripts/run_batch.sh 2>&1 | tail -40   # 13-row test file, defaults
#   ./scripts/run_batch.sh data/input/big.csv       # custom input file
#   ./scripts/run_batch.sh data/input/big.csv 8     # custom concurrency
#
# Environment variables (optional):
#   LLM_CONCURRENCY=4        — parallel LLM calls (match OLLAMA_NUM_PARALLEL)
#   OLLAMA_BASE_URL=http://localhost:11434
#   BATCH_CONCURRENCY=4      — default row concurrency
#   BATCH_SIZE=200            — checkpoint interval (rows per batch)
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
INPUT_FILE="${1:-data/input/test_addresses.xlsx}"
CONCURRENCY="${2:-4}"
LOGLEVEL="${3:-INFO}"

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
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

python -m src.batch_runner "$INPUT_FILE" \
    --concurrency "$CONCURRENCY" \
    --loglevel "$LOGLEVEL"
