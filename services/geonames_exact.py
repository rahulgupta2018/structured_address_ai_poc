"""
GeoNames exact-match validation (Step 3) with disambiguation.

Checks a town candidate against the normalised city-names index.
When multiple cities share the same name (e.g. Springfield), uses
disambiguation signals from the postal code lookup (admin1_code,
region name) to select the correct one.

When Step 1 detects a country mismatch (``mismatch_detected``), this
step tries the ``suggested_country_code`` FIRST, then falls back to the
original ``country_code``.  This avoids false positives (e.g. "Long" in
US when the address is actually in Thailand) and lets the pipeline
resolve the correct city deterministically.

Operates on session state dict:
  Reads:  state["libpostal_town"], state["libpostal_city_candidates"],
          state["postal_town_candidate"],
          state["postal_admin1_code"], state["postal_region"],
          state["country_code"],
          state["mismatch_detected"], state["suggested_country_code"]
  Writes: state["exact_match"], state["geonames_id"],
          state["town_candidate"], state["match_type"],
          state["match_confidence"]
"""

from __future__ import annotations

import logging

from services.geonames_repo import (
    resolve_all_cities_by_name,
    resolve_city_by_name,
    search_postal_by_place_name_cc,
)
from services.normalizer import normalize_for_matching
from utils.config import CONFIDENCE_EXACT_ALTERNATE, CONFIDENCE_EXACT_PRIMARY

logger = logging.getLogger(__name__)


def _disambiguate(
    matches: list[dict],
    postal_admin1_code: str | None,
    postal_region: str | None,
) -> dict:
    """Pick the best city from multiple same-name matches.

    Disambiguation priority:
      1. admin1_code matches postal_admin1_code  (strongest signal)
      2. Fall back to highest population          (existing behaviour)

    Args:
        matches: List of city dicts sorted by population (desc).
        postal_admin1_code: Admin1 code from postal lookup (e.g. "IL").
        postal_region: Admin1 region name from postal lookup (e.g. "Illinois").

    Returns:
        The best matching city dict.
    """
    if not matches:
        return {}

    # --- Signal 1: admin1 code from postal lookup -------------------------
    if postal_admin1_code:
        pac = postal_admin1_code.strip().upper()
        admin1_matches = [m for m in matches if (m.get("admin1_code") or "").upper() == pac]
        if len(admin1_matches) == 1:
            logger.debug(
                "Disambiguation: admin1_code '%s' → unique match %s (id=%d)",
                pac, admin1_matches[0]["name"], admin1_matches[0]["geonameid"],
            )
            return admin1_matches[0]
        if len(admin1_matches) > 1:
            # Multiple matches in the same admin1 — take largest by population
            logger.debug(
                "Disambiguation: admin1_code '%s' → %d matches, using population",
                pac, len(admin1_matches),
            )
            return admin1_matches[0]  # already sorted by pop desc

    # --- Fallback: highest population (original behaviour) ----------------
    logger.debug(
        "Disambiguation: no postal signal — defaulting to highest population (%s, pop=%d)",
        matches[0]["name"], matches[0].get("population", 0),
    )
    return matches[0]


def match(state: dict) -> dict:
    """Step 3: try exact GeoNames match on libpostal and postal candidates.

    Tries candidates in order:
        1. libpostal_town  (best guess from parser)
        2. remaining libpostal_city_candidates  (other city-like tokens)
        3. postal_town_candidate  (from postal-code DB lookup)

    When ``mismatch_detected`` is set by Step 1, tries the
    ``suggested_country_code`` first, then falls back to the original
    ``country_code``.  This resolves cross-country addresses
    deterministically instead of deferring to the LLM.

    When multiple cities share the same name, disambiguates using
    postal_admin1_code from Step 2. On first match, populates state
    and returns early.

    Returns:
        The mutated state dict.
    """
    country_code = state.get("country_code", "").upper()
    postal_admin1_code = state.get("postal_admin1_code")
    postal_region = state.get("postal_region")

    # Defaults
    state.setdefault("exact_match", False)
    state.setdefault("geonames_id", None)
    state.setdefault("town_candidate", None)
    state.setdefault("match_type", None)
    state.setdefault("match_confidence", 0.0)

    # Build country list: suggested CC first when mismatch detected
    countries: list[str] = []
    if state.get("mismatch_detected"):
        suggested = (state.get("suggested_country_code") or "").upper()
        if suggested and suggested != country_code:
            countries.append(suggested)
    countries.append(country_code)

    # Build ordered candidate list: primary pick, then other city tokens,
    # then postal-code hint.  Deduplicate while preserving order.
    seen: set[str] = set()
    candidates: list[str] = []
    for c in (
        [state.get("libpostal_town")]
        + (state.get("libpostal_city_candidates") or [])
        + [state.get("postal_town_candidate")]
    ):
        if c and c.strip() and c not in seen:
            seen.add(c)
            candidates.append(c)

    for cc in countries:
        for candidate in candidates:
            if not candidate or not candidate.strip():
                continue

            norm = normalize_for_matching(candidate)
            if not norm:
                continue

            # Fetch ALL matches so we can disambiguate
            all_matches = resolve_all_cities_by_name(cc, norm)
            if not all_matches:
                continue

            # Pick the best match via disambiguation
            if len(all_matches) == 1:
                city = all_matches[0]
            else:
                city = _disambiguate(all_matches, postal_admin1_code, postal_region)
                if not city:
                    continue

            # Determine match type
            norm_primary = normalize_for_matching(city["name"])
            norm_ascii = normalize_for_matching(city.get("ascii_name") or "")

            if norm == norm_primary:
                match_type = "primary"
            elif norm == norm_ascii:
                match_type = "ascii"
            else:
                match_type = "alternate"

            confidence = (
                CONFIDENCE_EXACT_PRIMARY
                if match_type in ("primary", "ascii")
                else CONFIDENCE_EXACT_ALTERNATE
            )

            state["exact_match"] = True
            state["geonames_id"] = city["geonameid"]
            # When matched via alternate name, preserve the input spelling
            # (e.g. "Morbi") rather than replacing with the GeoNames primary
            # ("Morvi"), since the input likely uses the current official name.
            state["town_candidate"] = (
                candidate if match_type == "alternate" else city["name"]
            )
            state["match_type"] = match_type
            state["match_confidence"] = confidence

            # Track which country was actually matched
            if cc != country_code:
                state["matched_country_code"] = cc

            logger.debug(
                "Exact match: '%s' → %s (id=%d, type=%s, conf=%.2f, cc=%s, disambig=%s)",
                candidate,
                city["name"],
                city["geonameid"],
                match_type,
                confidence,
                cc,
                "admin1" if postal_admin1_code else "population",
            )
            return state

    # ── Postal-code fallback ─────────────────────────────────────────
    # Towns too small for cities500 (e.g. Taxila/PK) may exist only in
    # the GeoNames postal-codes dataset.  Only try libpostal-derived
    # candidates — skip postal_town_candidate to avoid circular matches
    # (it already came from the postal table in Step 2).
    postal_town = state.get("postal_town_candidate")
    postal_candidates = [c for c in candidates if c != postal_town]
    for cc in countries:
        for candidate in postal_candidates:
            norm = normalize_for_matching(candidate)
            if not norm:
                continue
            postal_hits = search_postal_by_place_name_cc(norm, cc)
            if postal_hits:
                best = postal_hits[0]
                state["exact_match"] = True
                state["geonames_id"] = None
                state["town_candidate"] = best["place_name"]
                state["match_type"] = "postal"
                state["match_confidence"] = CONFIDENCE_EXACT_ALTERNATE
                if cc != country_code:
                    state["matched_country_code"] = cc
                logger.debug(
                    "Postal fallback: '%s' → %s (cc=%s)",
                    candidate, best["place_name"], cc,
                )
                return state

    return state
