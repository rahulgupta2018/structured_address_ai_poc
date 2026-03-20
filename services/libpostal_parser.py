"""
libpostal parser wrapper (Step 1).

Operates on session state dict:
  Reads:  state["raw_address"]
  Writes: state["libpostal_town"], state["libpostal_postal_code"],
          state["libpostal_street"], state["libpostal_building"],
          state["libpostal_city_candidates"],
          state["libpostal_country"]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
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

# Country name → ISO 3166-1 alpha-2 lookup, built from countriesV3.1.json.
# Indexes common name, official name, and altSpellings for broad coverage.
_COUNTRY_NAME_TO_CODE: dict[str, str] = {}

def _load_country_map() -> None:
    """Populate _COUNTRY_NAME_TO_CODE from the reference JSON (once)."""
    ref_path = Path(__file__).resolve().parent.parent / "data" / "reference" / "countriesV3.1.json"
    if not ref_path.exists():
        logger.warning("Country reference file not found: %s", ref_path)
        return
    with open(ref_path, encoding="utf-8") as fh:
        countries = json.load(fh)
    for entry in countries:
        cc = entry.get("cca2", "").upper()
        if not cc:
            continue
        # Common and official English names
        name_block = entry.get("name", {})
        for n in (name_block.get("common"), name_block.get("official")):
            if n:
                _COUNTRY_NAME_TO_CODE[n.strip().lower()] = cc
        # Alternate spellings / short forms
        for alt in entry.get("altSpellings", []):
            if alt and len(alt) > 2:  # skip the 2-letter code itself
                _COUNTRY_NAME_TO_CODE[alt.strip().lower()] = cc

_load_country_map()
logger.info("Loaded %d country name variants for lookup", len(_COUNTRY_NAME_TO_CODE))


def _country_name_to_code(name: str) -> Optional[str]:
    """Look up a country name and return its ISO alpha-2 code, or None."""
    return _COUNTRY_NAME_TO_CODE.get(name.strip().lower())


def parse(state: dict) -> dict:
    """Step 1: parse raw_address with libpostal and populate state.

    Reads:
        state["raw_address"]
    Writes:
        state["libpostal_town"]              — best city candidate (or None)
        state["libpostal_postal_code"]       — postal code (or None)
        state["libpostal_street"]            — street (or None)
        state["libpostal_building"]          — building/house number (or None)
        state["libpostal_city_candidates"]   — all city-like tokens (list)
        state["libpostal_country"]           — country name from address (or None)
        state["warnings"]                    — appended with any parse warnings

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
    state.setdefault("libpostal_city_candidates", [])
    state.setdefault("libpostal_country", None)

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
    country: Optional[str] = None

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
        elif label == "country" and country is None:
            country = value

    # Town candidate selection
    town: Optional[str] = None
    if not city_candidates:
        warnings.append("libpostal_no_city_label")
    elif len(city_candidates) == 1:
        town = city_candidates[0]
    else:
        # Prefer the *last* explicit "city" label — in most address formats
        # the actual city appears after sub-locality tokens like suburbs or
        # district names (e.g. "Long Beach Pra-Ae Beach, Krabi").
        for value, label in components:
            if label == "city" and value.strip():
                town = value.strip()  # keep scanning; last one wins
        if town is None:
            town = city_candidates[0]
        warnings.append("multiple_town_candidates")

    state["libpostal_town"] = town
    state["libpostal_postal_code"] = postal_code
    state["libpostal_street"] = street
    state["libpostal_building"] = building
    state["libpostal_city_candidates"] = city_candidates
    state["libpostal_country"] = country

    # Flag if libpostal detected a country that contradicts country_code
    if country:
        detected_cc = _country_name_to_code(country)
        input_cc = state.get("country_code", "").upper()
        if detected_cc and input_cc and detected_cc != input_cc:
            state["mismatch_detected"] = True
            state["suggested_country_code"] = detected_cc
            warnings.append("country_code_mismatch_in_address")
            logger.info(
                "Country mismatch: address contains '%s' (%s) but country_code is '%s'",
                country, detected_cc, input_cc,
            )

    return state
