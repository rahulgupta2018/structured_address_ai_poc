"""
GeoNames re-validation (Step 7).

Safety-net re-validation of the LLM-proposed town against GeoNames.
Runs only for LLM-path rows (Flow 3). Deterministic paths (Flow 1 /
Flow 2) skip this step — their confidence is set directly by the
DeterministicResolverAgent.

Operates on session state dict:
  Reads:  state["town_candidate"] or state["llm_result"],
          state["country_code"], state["suggested_country_code"],
          state["status"], state["match_confidence"]
  Writes: state["confidence"], state["status"]
"""

from __future__ import annotations

import logging
from typing import Optional

from rapidfuzz import fuzz, process

from services.geonames_repo import (
    get_all_normalized_names,
    list_countries_for_city,
    query_city,
    resolve_city_by_name,
    search_postal_by_place_name,
)
from services.normalizer import normalize_for_matching, redact_pii
from utils.config import (
    CONFIDENCE_LLM_CONFIRMED,
    CONFIDENCE_LLM_FUZZY_CONFIRMED,
    CONFIDENCE_LLM_UNVERIFIED,
    FUZZY_AMBIGUITY_MARGIN,
    FUZZY_MATCH_THRESHOLD,
)

logger = logging.getLogger(__name__)


def revalidate(state: dict) -> dict:
    """Step 7: re-validate the resolved town against GeoNames.

    For deterministic-path rows (exact_match or scan_match), this is a
    no-op pass-through — confidence was already set.

    For LLM-path rows, verifies the LLM's proposed town:
        1. Exact match in the given country
        2. Exact match in suggested country (if mismatch)
        3. Fuzzy match as fallback

    Returns:
        The mutated state dict.
    """
    status = state.get("status", "")

    # For deterministic-resolved rows, just pass through the confidence
    if status == "resolved":
        state["confidence"] = state.get("match_confidence", 0.0)
        return state

    # For LLM rows, validate the LLM result
    llm_result = state.get("llm_result")
    if not llm_result or not isinstance(llm_result, dict):
        # No LLM result — mark as needs_review
        state["confidence"] = 0.0
        state["status"] = "needs_review"
        return state

    town = llm_result.get("town", "")
    if not town:
        state["confidence"] = CONFIDENCE_LLM_UNVERIFIED
        state["status"] = "needs_review"
        return state

    country_code = state.get("country_code", "").upper()
    suggested_cc = state.get("suggested_country_code")

    # Try exact match in stated country
    norm = normalize_for_matching(town)
    city = resolve_city_by_name(country_code, norm)

    if city:
        # Check if the raw address contains a different (alternate) spelling
        # for the same city.  E.g. the address says "MORBI" but the LLM
        # returned "Morvi" (the GeoNames primary name).  We prefer the
        # address-text spelling because it's the name the sender used.
        resolved_name = _prefer_address_spelling(
            town, city, state.get("raw_address", ""), country_code
        )
        state["town_candidate"] = resolved_name
        state["geonames_id"] = city["geonameid"]
        state["confidence"] = CONFIDENCE_LLM_CONFIRMED
        state["status"] = "validated"
        logger.debug(
            "LLM revalidated (exact, %s): %s → %s",
            city.get("name_type", "?"), redact_pii(town), resolved_name,
        )
        return state

    # Try suggested country if mismatch was detected
    if suggested_cc:
        city = resolve_city_by_name(suggested_cc.upper(), norm)
        if city:
            state["town_candidate"] = city["name"]
            state["geonames_id"] = city["geonameid"]
            state["confidence"] = CONFIDENCE_LLM_CONFIRMED
            state["status"] = "validated"
            state["mismatch_detected"] = True
            state["suggested_country_code"] = suggested_cc
            logger.debug(
                "LLM revalidated (suggested cc %s): %s → %s",
                suggested_cc, redact_pii(town), city["name"],
            )
            return state

    # Try fuzzy match in stated country
    fuzzy_result = _fuzzy_revalidate(
        town, country_code, state.get("raw_address", "")
    )
    if fuzzy_result:
        state["town_candidate"] = fuzzy_result["name"]
        state["geonames_id"] = fuzzy_result["geonameid"]
        state["confidence"] = CONFIDENCE_LLM_FUZZY_CONFIRMED
        state["status"] = "validated"
        logger.debug(
            "LLM revalidated (fuzzy): %s → %s",
            redact_pii(town), fuzzy_result["name"],
        )
        return state

    # Cross-country fallback: if the town doesn't match in the stated
    # country, check ALL countries.  This catches cases like Barisardo
    # (IE→IT) where the LLM found the town but didn't set
    # suggested_country_code.
    cross_country_matches = list_countries_for_city(norm)
    if cross_country_matches:
        # Pick the best match (highest population)
        best = max(cross_country_matches, key=lambda m: m.get("population", 0))
        alt_cc = best.get("country_code", "")
        if alt_cc and alt_cc != country_code:
            # Verify exact match in the alternative country
            city = resolve_city_by_name(alt_cc, norm)
            if city:
                state["town_candidate"] = city["name"]
                state["geonames_id"] = city["geonameid"]
                state["confidence"] = CONFIDENCE_LLM_CONFIRMED
                state["status"] = "validated"
                state["mismatch_detected"] = True
                state["suggested_country_code"] = alt_cc
                logger.debug(
                    "LLM revalidated (cross-country %s→%s): %s → %s",
                    country_code, alt_cc, redact_pii(town), city["name"],
                )
                return state

    # ── Postal-code table fallback ─────────────────────────────────
    # Towns too small for cities1000 (e.g. Barisardo / "Bari Sardo")
    # may still appear in the GeoNames postal-codes dataset.
    postal_hits = search_postal_by_place_name(norm)
    if postal_hits:
        best_hit = postal_hits[0]
        hit_cc = best_hit.get("country_code", "")
        state["town_candidate"] = best_hit.get("place_name", town)
        state["confidence"] = CONFIDENCE_LLM_FUZZY_CONFIRMED
        state["status"] = "validated"
        if hit_cc and hit_cc != country_code:
            state["mismatch_detected"] = True
            state["suggested_country_code"] = hit_cc
        logger.debug(
            "LLM revalidated (postal fallback): %s → %s (cc=%s)",
            redact_pii(town), best_hit.get("place_name"), hit_cc,
        )
        return state

    # LLM proposed a town but GeoNames can't verify
    state["town_candidate"] = town
    state["confidence"] = CONFIDENCE_LLM_UNVERIFIED
    state["status"] = "needs_review"
    warnings = state.setdefault("warnings", [])
    warnings.append("geonames_no_match")
    return state


def _prefer_address_spelling(
    llm_town: str,
    city: dict,
    raw_address: str,
    country_code: str,
) -> str:
    """Return the address-text spelling of a city if it differs from the LLM.

    When the LLM returns the GeoNames primary name (e.g. "Morvi") but the
    raw address contains a valid alternate name for the *same* city
    (e.g. "MORBI"), prefer the address-text spelling — it's the name the
    sender actually used.

    Falls back to the LLM town name if no better match is found.
    """
    geonameid = city.get("geonameid")
    if not geonameid or not raw_address:
        return city.get("name", llm_town)

    # Tokenise the raw address and normalise
    addr_norm = normalize_for_matching(raw_address)
    tokens = set(addr_norm.replace(",", " ").replace("-", " ").split())
    if not tokens:
        return city.get("name", llm_town)

    llm_norm = normalize_for_matching(llm_town)

    # Check each token — if it's an alternate name for the same geonameid
    # and it's *different* from what the LLM returned, prefer it.
    for token in tokens:
        if len(token) < 3 or token == llm_norm:
            continue
        alt_city = resolve_city_by_name(country_code, token)
        if alt_city and alt_city.get("geonameid") == geonameid:
            # This address token maps to the same city — use it.
            # Title-case the token to get a proper name.
            logger.debug(
                "Preferring address spelling '%s' over LLM '%s' "
                "(same geonameid %s)",
                token.title(), llm_town, geonameid,
            )
            return token.title()

    # No alternate found in address — use whatever the LLM said
    # (or the GeoNames name if the LLM matched as alternate)
    if city.get("name_type") == "alternate":
        return llm_town
    return city.get("name", llm_town)


def _fuzzy_revalidate(
    town: str, country_code: str, raw_address: str
) -> Optional[dict]:
    """Attempt fuzzy match of an LLM-proposed town name."""
    all_names = get_all_normalized_names(country_code)
    if not all_names:
        return None

    norm = normalize_for_matching(town)
    if not norm or len(norm) < 3:
        return None

    city_list = [n for n in all_names if len(n) >= 3]

    results = process.extract(
        norm, city_list, scorer=fuzz.partial_ratio, limit=5
    )
    if not results:
        return None

    top_name, top_score, _ = results[0]

    if top_score < FUZZY_MATCH_THRESHOLD:
        return None

    tied = [r for r in results if top_score - r[1] < FUZZY_AMBIGUITY_MARGIN]

    if len(tied) == 1:
        winner_name = tied[0][0]
    elif raw_address:
        # Disambiguate using raw address token overlap
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
            return None
    else:
        return None

    city = resolve_city_by_name(country_code, winner_name)
    if not city:
        return None

    return city
