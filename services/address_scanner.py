"""
GeoNames raw-address scan (Step 5).

Scans the full normalised raw_address against the country-filtered city
lexicon to catch town names that libpostal missed. Uses exact n-gram
matching first, then fuzzy matching with strict acceptance rules.

When Step 1 detects a country mismatch (``mismatch_detected``), this
step tries the ``suggested_country_code`` FIRST, then falls back to the
original ``country_code``.

Operates on session state dict:
  Reads:  state["raw_address"], state["country_code"],
          state["mismatch_detected"], state["suggested_country_code"]
  Writes: state["scan_match"], state["scan_candidate"],
          state["geonames_id"], state["match_type"],
          state["match_confidence"]
"""

from __future__ import annotations

import logging
from typing import Optional

from rapidfuzz import fuzz, process

from services.geonames_repo import get_all_normalized_names, resolve_city_by_name
from services.normalizer import (
    extract_ngrams,
    normalize_for_matching,
    redact_pii,
    tokenize,
)
from utils.config import (
    CONFIDENCE_FUZZY_SCAN,
    FUZZY_AMBIGUITY_MARGIN,
    FUZZY_MATCH_THRESHOLD,
)

logger = logging.getLogger(__name__)

# Common address words / prepositions that happen to match place names.
# Single-token exact matches against these are skipped to avoid false positives
# (e.g. French "du" matching the commune Du, "de" matching De, etc.)
_STOPWORDS: set[str] = {
    # French / Spanish / Portuguese / Italian prepositions
    "de", "du", "des", "la", "le", "les", "au", "aux", "en",
    "el", "los", "las", "da", "do", "dos", "di", "del", "dal",
    # English
    "at", "by", "in", "of", "on", "to", "no",
    # German
    "an", "am", "im", "um", "zu",
    # General address terms
    "road", "st", "ave",
}


def scan(state: dict) -> dict:
    """Step 5: scan the raw address text for city names in GeoNames.

    When ``mismatch_detected`` is set by Step 1, tries the
    ``suggested_country_code`` first, then falls back to the original
    ``country_code``.

    Phases (per country):
        1. Exact n-gram matching against the country's city name set.
        2. Fuzzy matching (if no exact hit found).

    Returns:
        The mutated state dict.
    """
    state.setdefault("scan_match", False)
    state.setdefault("scan_candidate", None)

    raw_address = state.get("raw_address", "")
    country_code = state.get("country_code", "").upper()

    if not raw_address.strip() or not country_code:
        return state

    norm_address = normalize_for_matching(raw_address)
    tokens = tokenize(norm_address)

    if not tokens:
        return state

    ngrams = extract_ngrams(tokens, min_n=1, max_n=4)

    # Build country list: suggested CC first when mismatch detected
    countries: list[str] = []
    if state.get("mismatch_detected"):
        suggested = (state.get("suggested_country_code") or "").upper()
        if suggested and suggested != country_code:
            countries.append(suggested)
    countries.append(country_code)

    for cc in countries:
        all_names = get_all_normalized_names(cc)
        if not all_names:
            continue

        # ── Phase 1: Exact n-gram matching ───────────────────────────
        exact_hits: list[tuple[str, int]] = []  # (name, token_count)
        for ngram in ngrams:
            if ngram in all_names:
                n_tokens = len(ngram.split())
                # Skip single-word stopwords
                if n_tokens == 1 and ngram in _STOPWORDS:
                    logger.debug("Skipping stopword exact match: '%s'", ngram)
                    continue
                exact_hits.append((ngram, n_tokens))

        if exact_hits:
            # Prefer longest match
            exact_hits.sort(key=lambda x: x[1], reverse=True)
            best_name, best_len = exact_hits[0]

            # Ambiguity check for very short tokens
            similar = [
                h for h in exact_hits
                if h[0] != best_name and h[1] >= best_len - 1
            ]

            if best_len <= 1 and len(best_name) <= 3 and similar:
                logger.debug(
                    "Ambiguous short exact match '%s' — skipping",
                    best_name,
                )
            else:
                result = _resolve_scan(state, cc, best_name, "exact_token")
                if result.get("scan_match"):
                    return result

        # ── Phase 2: Fuzzy matching ──────────────────────────────────
        best_fuzzy = _fuzzy_scan(ngrams, all_names)
        if best_fuzzy is not None:
            result = _resolve_scan(state, cc, best_fuzzy, "fuzzy")
            if result.get("scan_match"):
                return result

    return state


def _fuzzy_scan(
    ngrams: list[str],
    city_names: set[str],
) -> Optional[str]:
    """Run fuzzy matching of address n-grams against city names."""
    city_list = list(city_names)

    best_name: Optional[str] = None
    best_score: float = 0.0
    runner_up_score: float = 0.0

    candidates = [ng for ng in ngrams if len(ng) >= 4]

    for ngram in candidates:
        results = process.extract(
            ngram, city_list, scorer=fuzz.ratio, limit=2
        )
        if not results:
            continue

        top_name, top_score, _ = results[0]

        if top_score < FUZZY_MATCH_THRESHOLD:
            continue

        second_score = results[1][1] if len(results) > 1 else 0.0

        if top_score - second_score < FUZZY_AMBIGUITY_MARGIN:
            continue

        if top_score > best_score:
            runner_up_score = best_score
            best_score = top_score
            best_name = top_name

    if best_name and (best_score - runner_up_score) < FUZZY_AMBIGUITY_MARGIN:
        logger.debug(
            "Global fuzzy ambiguity: %.1f vs %.1f", best_score, runner_up_score
        )
        return None

    return best_name


def _resolve_scan(
    state: dict,
    country_code: str,
    normalized_name: str,
    match_method: str,
) -> dict:
    """Look up the matched name and populate state."""
    city = resolve_city_by_name(country_code, normalized_name)
    if not city:
        return state

    state["scan_match"] = True
    state["scan_candidate"] = city["name"]
    state["geonames_id"] = city["geonameid"]
    state["match_type"] = match_method
    state["match_confidence"] = CONFIDENCE_FUZZY_SCAN

    logger.debug(
        "Scan match: '%s' → %s (method=%s)",
        redact_pii(normalized_name), city["name"], match_method,
    )
    return state
