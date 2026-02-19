"""
libpostal parser wrapper (Step 1).

Operates on session state dict:
  Reads:  state["raw_address"]
  Writes: state["libpostal_town"], state["libpostal_postal_code"],
          state["libpostal_street"], state["libpostal_building"]
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Graceful import — degrades if libpostal C library isn't installed
try:
    from postal.parser import parse_address as _postal_parse

    LIBPOSTAL_AVAILABLE = True
    logger.info("libpostal is available")
except ImportError:
    LIBPOSTAL_AVAILABLE = False
    logger.warning(
        "libpostal (postal) is not installed. "
        "Parser step will be skipped; rows fall through to scan/LLM."
    )

# Mapping from libpostal labels to our field names
_CITY_LABELS = {"city", "city_district", "suburb"}
_STREET_LABELS = {"road", "street"}
_BUILDING_LABELS = {"house_number", "house"}
_POSTCODE_LABELS = {"postcode", "postal_code"}


def parse(state: dict) -> dict:
    """Step 1: parse raw_address with libpostal and populate state.

    Reads:
        state["raw_address"]
    Writes:
        state["libpostal_town"]        — best city candidate (or None)
        state["libpostal_postal_code"] — postal code (or None)
        state["libpostal_street"]      — street (or None)
        state["libpostal_building"]    — building/house number (or None)
        state["warnings"]              — appended with any parse warnings

    Returns:
        The mutated state dict.
    """
    warnings: list[str] = state.setdefault("warnings", [])
    raw_address: str = state.get("raw_address", "")

    # Defaults
    state.setdefault("libpostal_town", None)
    state.setdefault("libpostal_postal_code", None)
    state.setdefault("libpostal_street", None)
    state.setdefault("libpostal_building", None)

    if not LIBPOSTAL_AVAILABLE:
        warnings.append("libpostal_not_installed")
        return state

    if not raw_address or not raw_address.strip():
        warnings.append("empty_address")
        return state

    try:
        components = _postal_parse(raw_address)
    except Exception as e:
        logger.error("libpostal parse error: %s", e)
        warnings.append("libpostal_parse_error")
        return state

    city_candidates: list[str] = []
    street: Optional[str] = None
    building: Optional[str] = None
    postal_code: Optional[str] = None

    for value, label in components:
        value = value.strip()
        if not value:
            continue

        if label in _CITY_LABELS:
            city_candidates.append(value)
        elif label in _STREET_LABELS and street is None:
            street = value
        elif label in _BUILDING_LABELS and building is None:
            building = value
        elif label in _POSTCODE_LABELS and postal_code is None:
            postal_code = value

    # Town candidate selection
    town: Optional[str] = None
    if not city_candidates:
        warnings.append("libpostal_no_city_label")
    elif len(city_candidates) == 1:
        town = city_candidates[0]
    else:
        # Prefer the explicit "city" label
        for value, label in components:
            if label == "city" and value.strip():
                town = value.strip()
                break
        if town is None:
            town = city_candidates[0]
        warnings.append("multiple_town_candidates")

    state["libpostal_town"] = town
    state["libpostal_postal_code"] = postal_code
    state["libpostal_street"] = street
    state["libpostal_building"] = building

    return state
