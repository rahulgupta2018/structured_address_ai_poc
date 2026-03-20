"""
Central configuration for the Structured Address AI pipeline.

All tuneable parameters, paths, and thresholds live here.
Reads from .env at the repository root (loaded automatically by ADK
or python-dotenv).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
REFERENCE_DIR = DATA_DIR / "reference"
SAMPLES_DIR = DATA_DIR / "samples"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"

GEONAMES_DB_PATH = DATA_DIR / "database" / "geonames.db"
GEONAMES_TSV_FILE = REFERENCE_DIR / "cities1000.txt"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _validated_int_env(name: str, default: int, min_val: int, max_val: int) -> int:
    """Read an integer env var with range validation."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid value for %s=%r (not an integer), using default %d",
            name, raw, default,
        )
        return default
    if not (min_val <= value <= max_val):
        logger.warning(
            "%s=%d is outside valid range [%d, %d], clamping",
            name, value, min_val, max_val,
        )
        return max(min_val, min(max_val, value))
    return value


def _validated_ollama_url(raw_url: str) -> str:
    """Validate the Ollama base URL to prevent SSRF."""
    parsed = urlparse(raw_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"OLLAMA_BASE_URL must use http or https scheme, got: {raw_url!r}"
        )
    if not parsed.hostname:
        raise ValueError(
            f"OLLAMA_BASE_URL must include a hostname, got: {raw_url!r}"
        )
    _ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
    hostname = parsed.hostname
    if (
        hostname not in _ALLOWED_HOSTS
        and not hostname.startswith("192.168.")
        and not hostname.startswith("10.")
    ):
        logger.warning(
            "OLLAMA_BASE_URL points to non-local host %r — ensure this is intentional",
            hostname,
        )
    return raw_url.rstrip("/")


# ── GeoNames matching ────────────────────────────────────────────────────────

FUZZY_MATCH_THRESHOLD: int = _validated_int_env(
    "FUZZY_MATCH_THRESHOLD", default=92, min_val=50, max_val=100
)
FUZZY_AMBIGUITY_MARGIN: int = _validated_int_env(
    "FUZZY_AMBIGUITY_MARGIN", default=5, min_val=1, max_val=50
)

# ── LLM configuration ───────────────────────────────────────────────────────

OLLAMA_BASE_URL: str = _validated_ollama_url(
    os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
)

# ADK model identifiers
LLM_MODEL_DEV: str = os.getenv("LLM_MODEL_DEV", "ollama_chat/qwen2.5-coder:14b")
LLM_MODEL_PROD: str = os.getenv("LLM_MODEL_PROD", "gemini-2.0-flash")
LLM_MODEL: str = os.getenv("LLM_MODEL", LLM_MODEL_DEV)

LLM_TEMPERATURE: float = 0.0
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "2048"))
LLM_TIMEOUT_SECONDS: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "180"))

# Ollama parallelism — must match the server's OLLAMA_NUM_PARALLEL setting.
# When running batch with concurrency > 1, this limits how many LLM calls
# are in-flight simultaneously so requests don't time out waiting in Ollama's
# queue.  Set to 1 for default Ollama, increase if you start Ollama with
# OLLAMA_NUM_PARALLEL=N.
LLM_CONCURRENCY: int = int(os.getenv("LLM_CONCURRENCY", "2"))

# Maximum LLM round-trips per row (tool calls + final answer).
# Thinking models (qwen3.5) resolve or fail within 2-3 turns;
# extra turns burn tokens without progress.
LLM_MAX_TURNS: int = int(os.getenv("LLM_MAX_TURNS", "2"))

# ── Confidence score weights ────────────────────────────────────────────────

CONFIDENCE_EXACT_PRIMARY: float = 1.00
CONFIDENCE_EXACT_ALTERNATE: float = 0.95
CONFIDENCE_FUZZY_SCAN: float = 0.80
CONFIDENCE_LLM_CONFIRMED: float = 0.75
CONFIDENCE_LLM_FUZZY_CONFIRMED: float = 0.70
CONFIDENCE_LLM_UNVERIFIED: float = 0.40
CONFIDENCE_REJECTED: float = 0.00

# ── Checkpointing (Dataflow batch) ──────────────────────────────────────────

CHECKPOINT_INTERVAL_ROWS: int = _validated_int_env(
    "CHECKPOINT_INTERVAL_ROWS", default=1000, min_val=100, max_val=100_000
)
