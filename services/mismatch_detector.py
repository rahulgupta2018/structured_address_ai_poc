"""
Country-code mismatch detector (Step 4).

Cross-checks a town candidate against other countries to detect
mismatches (e.g., "Barisardo" with country_code "IE" should be "IT").

Operates on session state dict:
  Reads:  state["libpostal_town"], state["town_candidate"],
          state["country_code"]
  Writes: state["mismatch_detected"], state["suggested_country_code"]
"""

from __future__ import annotations

import logging

from services.geonames_repo import list_countries_for_city
from services.normalizer import normalize_for_matching

logger = logging.getLogger(__name__)


def detect(state: dict) -> dict:
    """Step 4: detect country-code mismatches.

    If the town candidate was NOT found in the given country (exact_match
    is False), check whether it exists in other countries. If it does,
    flag a mismatch and suggest the most likely country.

    Returns:
        The mutated state dict.
    """
    state.setdefault("mismatch_detected", False)
    state.setdefault("suggested_country_code", None)

    # Only run if we don't already have an exact match
    if state.get("exact_match"):
        return state

    country_code = state.get("country_code", "").upper()

    # Try the best available candidate
    candidate = state.get("libpostal_town") or state.get("town_candidate")
    if not candidate or not candidate.strip():
        return state

    # Check if this town exists in other countries
    countries = list_countries_for_city(candidate)

    if not countries:
        return state

    # Filter out the current country
    other_countries = [
        c for c in countries if c["country_code"] != country_code
    ]

    if not other_countries:
        return state

    # The town exists in other countries but not in the stated one
    # Check that it truly doesn't exist in the stated country
    same_country = [
        c for c in countries if c["country_code"] == country_code
    ]

    if not same_country:
        # Town doesn't exist in stated country → mismatch
        best = max(other_countries, key=lambda c: c["population"])
        state["mismatch_detected"] = True
        state["suggested_country_code"] = best["country_code"]

        logger.info(
            "Mismatch detected: '%s' not in %s, suggested %s (pop=%d)",
            candidate, country_code,
            best["country_code"], best["population"],
        )

    return state
