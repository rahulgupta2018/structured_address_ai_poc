"""
Central configuration for the Structured Address AI pipeline.
All tuneable parameters, paths, and thresholds live here.
"""

from pathlib import Path
from urllib.parse import urlparse
import logging
import os

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
REFERENCE_DIR = DATA_DIR / "reference"
SAMPLES_DIR = DATA_DIR / "samples"
OUTPUT_DIR = DATA_DIR / "output"

GEONAMES_FILE = REFERENCE_DIR / "cities5000.txt"


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
    # Allow only localhost / 127.x / private-range hosts in POC
    _ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
    hostname = parsed.hostname
    if hostname not in _ALLOWED_HOSTS and not hostname.startswith("192.168.") and not hostname.startswith("10."):
        logger.warning(
            "OLLAMA_BASE_URL points to non-local host %r — ensure this is intentional",
            hostname,
        )
    return raw_url.rstrip("/")


# ── GeoNames matching ────────────────────────────────────────────────────────
# Fuzzy match threshold for the raw-address scan step (0–100).
# 92 is a starting point; tune empirically on a test set.
FUZZY_MATCH_THRESHOLD: int = _validated_int_env("FUZZY_MATCH_THRESHOLD", default=92, min_val=50, max_val=100)

# Minimum margin between the top two fuzzy candidates to accept a match.
# If the gap is smaller than this, the match is considered ambiguous.
FUZZY_AMBIGUITY_MARGIN: int = _validated_int_env("FUZZY_AMBIGUITY_MARGIN", default=5, min_val=1, max_val=50)

# ── LLM fallback (Ollama) ───────────────────────────────────────────────────
OLLAMA_BASE_URL: str = _validated_ollama_url(
    os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
)
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:14b")
LLM_TEMPERATURE: float = 0.0
LLM_MAX_TOKENS: int = 256
LLM_TIMEOUT_SECONDS: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
LLM_MAX_RETRIES: int = 3
LLM_BATCH_SIZE: int = int(os.getenv("LLM_BATCH_SIZE", "10"))
LLM_CONCURRENCY: int = _validated_int_env("LLM_CONCURRENCY", default=4, min_val=1, max_val=16)

# ── Confidence score weights ────────────────────────────────────────────────
CONFIDENCE_EXACT_PRIMARY: float = 1.00
CONFIDENCE_EXACT_ALTERNATE: float = 0.95
CONFIDENCE_FUZZY_SCAN: float = 0.80
CONFIDENCE_LLM_CONFIRMED: float = 0.75
CONFIDENCE_LLM_FUZZY_CONFIRMED: float = 0.70
CONFIDENCE_LLM_UNVERIFIED: float = 0.40
CONFIDENCE_REJECTED: float = 0.00
