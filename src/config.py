"""
Central configuration for the Structured Address AI pipeline.
All tuneable parameters, paths, and thresholds live here.
"""

from pathlib import Path
import os

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
REFERENCE_DIR = DATA_DIR / "reference"
SAMPLES_DIR = DATA_DIR / "samples"
OUTPUT_DIR = DATA_DIR / "output"

GEONAMES_FILE = REFERENCE_DIR / "cities5000.txt"

# ── GeoNames matching ────────────────────────────────────────────────────────
# Fuzzy match threshold for the raw-address scan step (0–100).
# 92 is a starting point; tune empirically on a test set.
FUZZY_MATCH_THRESHOLD: int = int(os.getenv("FUZZY_MATCH_THRESHOLD", "92"))

# Minimum margin between the top two fuzzy candidates to accept a match.
# If the gap is smaller than this, the match is considered ambiguous.
FUZZY_AMBIGUITY_MARGIN: int = int(os.getenv("FUZZY_AMBIGUITY_MARGIN", "5"))

# ── LLM fallback (Ollama) ───────────────────────────────────────────────────
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:14b")
LLM_TEMPERATURE: float = 0.0
LLM_MAX_TOKENS: int = 256
LLM_TIMEOUT_SECONDS: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
LLM_MAX_RETRIES: int = 3
LLM_BATCH_SIZE: int = int(os.getenv("LLM_BATCH_SIZE", "10"))

# ── Confidence score weights ────────────────────────────────────────────────
CONFIDENCE_EXACT_PRIMARY: float = 1.00
CONFIDENCE_EXACT_ALTERNATE: float = 0.95
CONFIDENCE_FUZZY_SCAN: float = 0.80
CONFIDENCE_LLM_CONFIRMED: float = 0.75
CONFIDENCE_LLM_FUZZY_CONFIRMED: float = 0.70
CONFIDENCE_LLM_UNVERIFIED: float = 0.40
CONFIDENCE_REJECTED: float = 0.00
