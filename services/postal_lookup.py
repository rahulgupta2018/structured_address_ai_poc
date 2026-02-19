"""
Postal code lookup (Step 2).

Queries the GeoNames postal_codes table to find the place name
associated with a postal code + country.

Operates on session state dict:
  Reads:  state["libpostal_postal_code"], state["country_code"]
  Writes: state["postal_town_candidate"]  — place name from postal DB
          state["postal_admin1_code"]     — admin1 code (e.g. "IL")
          state["postal_region"]          — admin1 region name (e.g. "Illinois")
          state["postal_city_hint"]       — same as postal_town_candidate (alias for clarity)
"""

from __future__ import annotations

import logging

from services.geonames_repo import query_postal_code

logger = logging.getLogger(__name__)


def lookup(state: dict) -> dict:
    """Step 2: look up postal code in GeoNames.

    If a postal code was extracted by libpostal, query the database
    for the associated place name and admin1 region. These provide
    disambiguation signals for Step 3 (exact match).

    Returns:
        The mutated state dict.
    """
    postal_code = state.get("libpostal_postal_code")
    country_code = state.get("country_code", "")

    state.setdefault("postal_town_candidate", None)
    state.setdefault("postal_admin1_code", None)
    state.setdefault("postal_region", None)
    state.setdefault("postal_city_hint", None)

    if not postal_code or not country_code:
        return state

    results = query_postal_code(postal_code, country_code)

    if results:
        first = results[0]
        state["postal_town_candidate"] = first["place_name"]
        state["postal_admin1_code"] = first.get("admin_code1") or None
        state["postal_region"] = first.get("admin_name1") or None
        state["postal_city_hint"] = first["place_name"]

        logger.debug(
            "Postal lookup: %s/%s → %s (admin1=%s / %s)",
            country_code,
            postal_code,
            first["place_name"],
            first.get("admin_code1"),
            first.get("admin_name1"),
        )

    return state
