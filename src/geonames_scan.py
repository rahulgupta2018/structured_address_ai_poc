"""
Deterministic GeoNames raw-address scan (Step 3).

Scans the full normalized raw_address against the country-filtered city
lexicon to catch town names that libpostal missed or mislabeled.
Uses exact token/phrase matching first, then fuzzy matching with strict
acceptance rules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz, process

from src.config import (
    CONFIDENCE_FUZZY_SCAN,
    FUZZY_AMBIGUITY_MARGIN,
    FUZZY_MATCH_THRESHOLD,
)
from src.geonames_loader import GeoNamesIndex
from src.preprocess import extract_ngrams, normalize_for_matching, tokenize
from src.schemas import GeoNamesMatch

logger = logging.getLogger(__name__)


@dataclass
class ScanCandidate:
    """A potential match found during the raw-address scan."""

    normalized_name: str
    score: float
    match_method: str  # "exact_token" or "fuzzy"


def scan_raw_address(
    index: GeoNamesIndex,
    raw_address: str,
    country_code: str,
) -> GeoNamesMatch:
    """
    Scan the full raw address text against the GeoNames city lexicon for the
    given country. Tries exact token/phrase match first, then fuzzy.

    Args:
        index: Pre-loaded GeoNamesIndex.
        raw_address: The concatenated, un-normalized address string.
        country_code: ISO 3166-1 alpha-2 country code.

    Returns:
        GeoNamesMatch with matched=True if an unambiguous match is found.
    """
    cc = country_code.upper()
    all_names = index.get_all_names(cc)

    if not all_names or not raw_address.strip():
        return GeoNamesMatch(matched=False)

    norm_address = normalize_for_matching(raw_address)
    tokens = tokenize(norm_address)

    if not tokens:
        return GeoNamesMatch(matched=False)

    # ── Phase 1: Exact token / phrase matching ───────────────────────────
    # Generate n-grams from the address (1 to 4 tokens) and check each
    # against the GeoNames name set for the country.
    ngrams = extract_ngrams(tokens, min_n=1, max_n=4)

    exact_hits: list[ScanCandidate] = []
    for ngram in ngrams:
        if ngram in all_names:
            exact_hits.append(
                ScanCandidate(
                    normalized_name=ngram,
                    score=100.0,
                    match_method="exact_token",
                )
            )

    if exact_hits:
        # Prefer the longest exact match (multi-word city names are more specific)
        exact_hits.sort(key=lambda c: len(c.normalized_name), reverse=True)
        best = exact_hits[0]

        # Check if there are other long matches of similar length (ambiguity)
        similar = [
            h
            for h in exact_hits
            if h.normalized_name != best.normalized_name
            and len(h.normalized_name) >= len(best.normalized_name) - 2
        ]

        # If the best exact hit is a very short token (≤3 chars) and there are
        # alternatives, it's likely ambiguous (e.g., "bad" matching a city)
        if len(best.normalized_name) <= 3 and similar:
            logger.debug(
                "Ambiguous short exact match '%s' for %s — skipping",
                best.normalized_name,
                cc,
            )
        else:
            return _resolve_match(index, cc, best.normalized_name, "exact_token")

    # ── Phase 2: Fuzzy matching ──────────────────────────────────────────
    # Only try fuzzy if no exact match was found.
    # We match each n-gram from the address against the city name set.
    best_fuzzy = _fuzzy_scan(ngrams, all_names)

    if best_fuzzy is not None:
        return _resolve_match(index, cc, best_fuzzy.normalized_name, "fuzzy")

    return GeoNamesMatch(matched=False)


def _fuzzy_scan(
    ngrams: list[str],
    city_names: set[str],
) -> Optional[ScanCandidate]:
    """
    Run fuzzy matching of address n-grams against the city name set.
    Only accepts results above the threshold with clear margin from runner-up.
    """
    city_list = list(city_names)  # rapidfuzz needs a sequence

    best_candidate: Optional[ScanCandidate] = None
    best_score: float = 0.0
    runner_up_score: float = 0.0

    # Only fuzzy-match n-grams of length ≥ 4 chars to avoid noisy short matches
    candidates = [ng for ng in ngrams if len(ng) >= 4]

    for ngram in candidates:
        results = process.extract(
            ngram,
            city_list,
            scorer=fuzz.ratio,
            limit=2,
        )

        if not results:
            continue

        top_name, top_score, _ = results[0]

        if top_score < FUZZY_MATCH_THRESHOLD:
            continue

        second_score = results[1][1] if len(results) > 1 else 0.0

        # Check ambiguity margin
        if top_score - second_score < FUZZY_AMBIGUITY_MARGIN:
            continue  # too close — ambiguous

        if top_score > best_score:
            runner_up_score = best_score
            best_score = top_score
            best_candidate = ScanCandidate(
                normalized_name=top_name,
                score=top_score,
                match_method="fuzzy",
            )

    # Final ambiguity check across all n-gram results
    if best_candidate and (best_score - runner_up_score) < FUZZY_AMBIGUITY_MARGIN:
        logger.debug("Global fuzzy ambiguity: %.1f vs %.1f", best_score, runner_up_score)
        return None

    return best_candidate


def _resolve_match(
    index: GeoNamesIndex,
    country_code: str,
    normalized_name: str,
    match_method: str,
) -> GeoNamesMatch:
    """Look up the matched normalized name and return a GeoNamesMatch."""
    cities = index.get_cities(country_code, normalized_name)
    if not cities:
        return GeoNamesMatch(matched=False)

    best = max(cities, key=lambda c: c.population)

    return GeoNamesMatch(
        matched=True,
        geonames_id=best.geonames_id,
        matched_name=best.name,
        match_type=match_method,
        confidence=CONFIDENCE_FUZZY_SCAN,
    )
