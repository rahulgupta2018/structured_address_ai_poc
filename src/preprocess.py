"""
Text preprocessing and normalization for address strings.
All matching-oriented transforms happen here; original text is never mutated.
"""

from __future__ import annotations

import re
import unicodedata

from unidecode import unidecode


# Pre-compiled patterns
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_unicode(text: str) -> str:
    """Apply NFKC unicode normalization."""
    return unicodedata.normalize("NFKC", text)


def collapse_whitespace(text: str) -> str:
    """Replace runs of whitespace (including tabs/newlines) with a single space."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalize_punctuation(text: str) -> str:
    """Convert full-width punctuation to ASCII equivalents."""
    # Full-width comma → comma, full-width period → period, etc.
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
    """
    Full normalization pipeline for matching purposes.
    Returns a casefolded, whitespace-collapsed, NFKC-normalized string.
    """
    text = normalize_unicode(text)
    text = normalize_punctuation(text)
    text = collapse_whitespace(text)
    text = casefold(text)
    return text


def build_raw_address(lines: list[str]) -> str:
    """
    Concatenate non-empty address lines into a single raw address string.
    """
    return " ".join(line.strip() for line in lines if line and line.strip())


def tokenize(text: str) -> list[str]:
    """
    Simple whitespace tokenizer on normalized text.
    Returns list of tokens (no empty strings).
    """
    return [t for t in text.split() if t]


def extract_ngrams(tokens: list[str], min_n: int = 1, max_n: int = 4) -> list[str]:
    """
    Generate contiguous n-grams from a token list.
    Used by the GeoNames scan step to match multi-word city names.

    Args:
        tokens: list of whitespace-split tokens
        min_n: minimum n-gram size
        max_n: maximum n-gram size

    Returns:
        list of n-gram strings (space-joined)
    """
    ngrams: list[str] = []
    for n in range(min_n, min(max_n + 1, len(tokens) + 1)):
        for i in range(len(tokens) - n + 1):
            ngrams.append(" ".join(tokens[i : i + n]))
    return ngrams


# ── PII redaction ────────────────────────────────────────────────────────────

def redact_pii(text: str | None, keep_chars: int = 5) -> str:
    """
    Redact a string for safe logging, preserving the first few characters.

    Examples:
        "Marienplatz 1, München"  →  "Marie…[redacted]"
        None                      →  "<empty>"
        ""                        →  "<empty>"
    """
    if not text or not text.strip():
        return "<empty>"
    text = text.strip()
    if len(text) <= keep_chars:
        return text + "…"
    return text[:keep_chars] + "…[redacted]"
