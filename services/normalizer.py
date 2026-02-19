"""
Text preprocessing and normalization (Step 0).

Operates on session state dict:
  Reads:  state["address_1"], state["address_2"], state["address_3"]
  Writes: state["raw_address"], state["normalized"]
"""

from __future__ import annotations

import re
import unicodedata

from unidecode import unidecode

# Pre-compiled patterns
_WHITESPACE_RE = re.compile(r"\s+")


# ── Pure text transforms ─────────────────────────────────────────────────────


def normalize_unicode(text: str) -> str:
    """Apply NFKC unicode normalization."""
    return unicodedata.normalize("NFKC", text)


def collapse_whitespace(text: str) -> str:
    """Replace runs of whitespace with a single space."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalize_punctuation(text: str) -> str:
    """Convert full-width punctuation to ASCII equivalents."""
    mapping = {
        "\uff0c": ",",
        "\uff0e": ".",
        "\uff1a": ":",
        "\uff1b": ";",
        "\u3001": ",",
        "\u3002": ".",
    }
    for fw, hw in mapping.items():
        text = text.replace(fw, hw)
    return text


def casefold(text: str) -> str:
    """Casefold for case-insensitive matching."""
    return text.casefold()


def to_ascii(text: str) -> str:
    """Transliterate to ASCII using Unidecode (lossy)."""
    return unidecode(text)


def normalize_for_matching(text: str) -> str:
    """Full normalization pipeline for matching purposes."""
    text = normalize_unicode(text)
    text = normalize_punctuation(text)
    text = collapse_whitespace(text)
    text = casefold(text)
    return text


def build_raw_address(lines: list[str]) -> str:
    """Concatenate non-empty address lines into a single string."""
    return " ".join(line.strip() for line in lines if line and line.strip())


def tokenize(text: str) -> list[str]:
    """Simple whitespace tokenizer on normalized text."""
    return [t for t in text.split() if t]


def extract_ngrams(tokens: list[str], min_n: int = 1, max_n: int = 4) -> list[str]:
    """Generate contiguous n-grams from a token list."""
    ngrams: list[str] = []
    for n in range(min_n, min(max_n + 1, len(tokens) + 1)):
        for i in range(len(tokens) - n + 1):
            ngrams.append(" ".join(tokens[i : i + n]))
    return ngrams


def redact_pii(text: str | None, keep_chars: int = 5) -> str:
    """Redact a string for safe logging, preserving first few characters."""
    if not text or not text.strip():
        return "<empty>"
    text = text.strip()
    if len(text) <= keep_chars:
        return text + "…"
    return text[:keep_chars] + "…[redacted]"


# ── State-based entry point ──────────────────────────────────────────────────


def preprocess(state: dict) -> dict:
    """Step 0: build raw_address from address lines, produce normalized form.

    Reads:
        state["address_1"], state["address_2"], state["address_3"]
    Writes:
        state["raw_address"]  — concatenated address lines
        state["normalized"]   — normalized-for-matching form
        state["warnings"]     — appends if no address data

    Returns:
        The mutated state dict.
    """
    lines = [
        state.get("address_1") or "",
        state.get("address_2") or "",
        state.get("address_3") or "",
    ]

    raw = build_raw_address(lines)
    state["raw_address"] = raw
    state["normalized"] = normalize_for_matching(raw) if raw else ""

    if not raw:
        warnings = state.setdefault("warnings", [])
        warnings.append("no_address_data")

    return state
