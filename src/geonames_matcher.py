"""
GeoNames exact-match validation.

Step 2 of the pipeline: given a town candidate and country code, check for
an exact normalized match against the GeoNames index.
"""

from __future__ import annotations

import logging

from rapidfuzz import fuzz, process

from .config import (
    CONFIDENCE_EXACT_ALTERNATE,
    CONFIDENCE_EXACT_PRIMARY,
    CONFIDENCE_LLM_FUZZY_CONFIRMED,
    FUZZY_AMBIGUITY_MARGIN,
    FUZZY_MATCH_THRESHOLD,
)
from .geonames_loader import GeoNamesIndex
from .preprocess import normalize_for_matching, redact_pii
from .schemas import GeoNamesMatch

logger = logging.getLogger(__name__)


def match_exact(
    index: GeoNamesIndex,
    town_candidate: str,
    country_code: str,
) -> GeoNamesMatch:
    """
    Attempt an exact normalized match of town_candidate against GeoNames.

    Matching order:
    1. Primary name
    2. ASCII name
    3. Alternate names

    All are already indexed by normalized form, so this is a single dict lookup.
    When multiple cities share the same normalized name within a country,
    we pick the one with the highest population (most likely correct).

    Args:
        index: Pre-loaded GeoNamesIndex.
        town_candidate: The raw town string to validate.
        country_code: ISO 3166-1 alpha-2 country code.

    Returns:
        GeoNamesMatch with matched=True if found, else matched=False.
    """
    if not town_candidate or not town_candidate.strip():
        return GeoNamesMatch(matched=False)

    cc = country_code.upper()
    norm = normalize_for_matching(town_candidate)

    if not norm:
        return GeoNamesMatch(matched=False)

    cities = index.get_cities(cc, norm)
    if not cities:
        return GeoNamesMatch(matched=False)

    # Pick the best match: prefer highest population for disambiguation
    best = max(cities, key=lambda c: c.population)

    # Determine match type
    norm_primary = normalize_for_matching(best.name)
    norm_ascii = normalize_for_matching(best.ascii_name)

    if norm == norm_primary:
        match_type = "primary"
    elif norm == norm_ascii:
        match_type = "ascii"
    else:
        match_type = "alternate"

    # Confidence based on match type
    confidence = (
        CONFIDENCE_EXACT_PRIMARY
        if match_type in ("primary", "ascii")
        else CONFIDENCE_EXACT_ALTERNATE
    )

    return GeoNamesMatch(
        matched=True,
        geonames_id=best.geonames_id,
        matched_name=best.name,
        match_type=match_type,
        confidence=confidence,
    )


def match_fuzzy(
    index: GeoNamesIndex,
    town_candidate: str,
    country_code: str,
    raw_address: str = "",
) -> GeoNamesMatch:
    """
    Attempt a fuzzy match of town_candidate against GeoNames.

    Used as a fallback when match_exact fails on an LLM-proposed candidate.
    The LLM may return an abbreviated or partial name (e.g., "St-Etienne")
    that is a substring or close variant of the official GeoNames name
    (e.g., "Court-Saint-Étienne").

    Strategy:
    1. Use partial_ratio to find GeoNames names that *contain* the candidate
       as a substring (handles abbreviations like st→saint).
    2. When multiple candidates score equally, disambiguate by checking which
       GeoNames name has the most token overlap with the full raw address.

    Args:
        index: Pre-loaded GeoNamesIndex.
        town_candidate: The LLM-proposed town string.
        country_code: ISO 3166-1 alpha-2 country code.
        raw_address: The full raw address text (used for disambiguation).

    Returns:
        GeoNamesMatch with matched=True if an unambiguous fuzzy match is found.
    """
    if not town_candidate or not town_candidate.strip():
        return GeoNamesMatch(matched=False)

    cc = country_code.upper()
    all_names = index.get_all_names(cc)

    if not all_names:
        return GeoNamesMatch(matched=False)

    norm = normalize_for_matching(town_candidate)
    if not norm or len(norm) < 3:
        return GeoNamesMatch(matched=False)

    city_list = list(all_names)

    # Filter out very short names (≤2 chars) — they cause false positives
    # with partial_ratio (e.g., single-char names score 100 against anything)
    city_list = [n for n in city_list if len(n) >= 3]

    # partial_ratio: finds the best partial substring alignment.
    # "st-etienne" vs "court-saint-etienne" → 94.7 (finds "etienne" overlap)
    results = process.extract(
        norm,
        city_list,
        scorer=fuzz.partial_ratio,
        limit=5,
    )

    if not results:
        return GeoNamesMatch(matched=False)

    top_name, top_score, _ = results[0]

    # Must meet threshold
    if top_score < FUZZY_MATCH_THRESHOLD:
        logger.debug(
            "LLM fuzzy re-validation below threshold: '%s' best='%s' score=%.1f",
            redact_pii(norm), redact_pii(top_name), top_score,
        )
        return GeoNamesMatch(matched=False)

    # Gather all results that scored within the ambiguity margin of the top
    tied = [r for r in results if top_score - r[1] < FUZZY_AMBIGUITY_MARGIN]

    if len(tied) == 1:
        # Clear winner — no ambiguity
        winner_name = tied[0][0]
    elif raw_address:
        # Disambiguate using the raw address: which GeoNames name has the
        # most token overlap with the full address text?
        norm_addr = normalize_for_matching(raw_address)
        addr_tokens = set(norm_addr.replace("-", " ").split())

        best_overlap = -1
        winner_name = None
        for name, score, _ in tied:
            name_tokens = set(name.replace("-", " ").split())
            overlap = len(name_tokens & addr_tokens)
            if overlap > best_overlap:
                best_overlap = overlap
                winner_name = name

        if winner_name is None:
            return GeoNamesMatch(matched=False)

        logger.debug(
            "LLM fuzzy disambiguated via raw address: '%s' (overlap=%d tokens)",
            redact_pii(winner_name), best_overlap,
        )
    else:
        # Ambiguous with no raw address to disambiguate
        logger.debug(
            "LLM fuzzy re-validation ambiguous: '%s' — %d tied candidates",
            redact_pii(norm), len(tied),
        )
        return GeoNamesMatch(matched=False)

    # Resolve the matched name to a CityRecord
    cities = index.get_cities(cc, winner_name)
    if not cities:
        return GeoNamesMatch(matched=False)

    best = max(cities, key=lambda c: c.population)

    logger.info(
        "LLM fuzzy re-validation matched: '%s' → '%s' (score=%.1f, id=%d)",
        redact_pii(town_candidate), redact_pii(best.name), top_score, best.geonames_id,
    )

    return GeoNamesMatch(
        matched=True,
        geonames_id=best.geonames_id,
        matched_name=best.name,
        match_type="llm_fuzzy",
        confidence=CONFIDENCE_LLM_FUZZY_CONFIRMED,
    )
