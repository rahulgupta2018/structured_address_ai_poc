"""
Postal code lookup (Step 2).

Queries the GeoNames postal_codes table to find the place name
associated with a postal code + country.

Lookup strategy (in order):
  1. Primary:           postal_code + country_code
  2. Suggested-country: postal_code + suggested_country_code (from Step 1
     mismatch detection), tried only when primary returns nothing and a
     suggested country exists that differs from the input country.
  3. Postal-only:       postal_code without any country filter, then
     disambiguated by cross-referencing with suggested_country_code, or
     accepted if all rows point to a single country.

Operates on session state dict:
  Reads:  state["libpostal_postal_code"], state["country_code"],
          state["suggested_country_code"] (optional, from Step 1)
  Writes: state["postal_town_candidate"]  — place name from postal DB
          state["postal_admin1_code"]     — admin1 code (e.g. "IL")
          state["postal_region"]          — admin1 region name (e.g. "Illinois")
          state["postal_city_hint"]       — same as postal_town_candidate (alias)
          state["postal_lookup_method"]   — which strategy matched:
              "primary" | "suggested_country" | "postal_only" | None
"""

from __future__ import annotations

import logging

from services.geonames_repo import query_postal_code, query_postal_code_any_country

logger = logging.getLogger(__name__)


def lookup(state: dict) -> dict:
    """Step 2: look up postal code in GeoNames.

    If a postal code was extracted by libpostal, query the database
    for the associated place name and admin1 region. These provide
    disambiguation signals for Step 3 (exact match).

    When the primary lookup (postal + country) finds nothing, two
    fallback strategies are attempted — see module docstring for details.

    Returns:
        The mutated state dict.
    """
    postal_code = state.get("libpostal_postal_code")
    country_code = state.get("country_code", "")
    suggested_cc = state.get("suggested_country_code")

    state.setdefault("postal_town_candidate", None)
    state.setdefault("postal_admin1_code", None)
    state.setdefault("postal_region", None)
    state.setdefault("postal_city_hint", None)
    state.setdefault("postal_lookup_method", None)

    if not postal_code:
        return state

    # ── 1. Primary: postal_code + country_code ──────────────────────────
    if country_code:
        results = query_postal_code(postal_code, country_code)
        if results:
            _apply(state, results[0], method="primary")
            return state

    # ── 2. Suggested-country fallback ───────────────────────────────────
    if suggested_cc and suggested_cc != country_code:
        results = query_postal_code(postal_code, suggested_cc)
        if results:
            _apply(state, results[0], method="suggested_country")
            return state

    # ── 3. Postal-only fallback (no country filter) ─────────────────────
    all_results = query_postal_code_any_country(postal_code)
    if not all_results:
        return state

    countries_found = {r["country_code"] for r in all_results}

    # 3a. Cross-reference with suggested_country_code
    if suggested_cc and suggested_cc in countries_found:
        filtered = [r for r in all_results if r["country_code"] == suggested_cc]
        _apply(state, filtered[0], method="postal_only")
        return state

    # 3b. If all results point to a single country, use them
    if len(countries_found) == 1:
        _apply(state, all_results[0], method="postal_only")
        return state

    # 3c. Ambiguous — multiple countries, no suggested_cc to disambiguate
    logger.info(
        "Postal-only fallback ambiguous: %s found in %d countries %s",
        postal_code, len(countries_found), sorted(countries_found),
    )
    return state


def _apply(state: dict, row: dict, *, method: str) -> None:
    """Write postal lookup results into state from a DB row."""
    state["postal_town_candidate"] = row["place_name"]
    state["postal_admin1_code"] = row.get("admin_code1") or None
    state["postal_region"] = row.get("admin_name1") or None
    state["postal_city_hint"] = row["place_name"]
    state["postal_lookup_method"] = method

    logger.debug(
        "Postal lookup (%s): %s → %s (admin1=%s / %s)",
        method,
        row.get("postal_code"),
        row["place_name"],
        row.get("admin_code1"),
        row.get("admin_name1"),
    )
