"""
libpostal parser wrapper (Step 1).

Wraps the `postal` Python bindings to extract structured address components.
Falls back gracefully if libpostal is not installed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Try to import postal; if unavailable, the module degrades gracefully.
try:
    from postal.parser import parse_address as _postal_parse

    LIBPOSTAL_AVAILABLE = True
    logger.info("libpostal is available")
except ImportError:
    LIBPOSTAL_AVAILABLE = False
    logger.warning(
        "libpostal (postal) is not installed. "
        "The parser step will be skipped and all rows will fall through "
        "to GeoNames scan / LLM fallback."
    )


# Mapping from libpostal labels to our output field names
_CITY_LABELS = {"city", "city_district", "suburb"}
_STREET_LABELS = {"road", "street"}
_BUILDING_LABELS = {"house_number", "house"}
_POSTCODE_LABELS = {"postcode", "postal_code"}


@dataclass
class ParseResult:
    """Structured output from libpostal parsing."""

    town_candidate: Optional[str] = None
    street: Optional[str] = None
    building: Optional[str] = None
    postal_code: Optional[str] = None
    raw_components: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def parse_address(raw_address: str) -> ParseResult:
    """
    Parse a raw address string using libpostal.

    If libpostal is not installed, returns an empty ParseResult with a warning.

    Args:
        raw_address: The concatenated address string.

    Returns:
        ParseResult with extracted components.
    """
    result = ParseResult()

    if not LIBPOSTAL_AVAILABLE:
        result.warnings.append("libpostal_not_installed")
        return result

    if not raw_address or not raw_address.strip():
        result.warnings.append("empty_address")
        return result

    try:
        components = _postal_parse(raw_address)
    except Exception as e:
        logger.error("libpostal parse error: %s", e)
        result.warnings.append("libpostal_parse_error")
        return result

    # Components is a list of (value, label) tuples
    city_candidates: list[str] = []

    for value, label in components:
        value = value.strip()
        if not value:
            continue

        result.raw_components[label] = value

        if label in _CITY_LABELS:
            city_candidates.append(value)
        elif label in _STREET_LABELS:
            result.street = value
        elif label in _BUILDING_LABELS:
            result.building = value
        elif label in _POSTCODE_LABELS:
            result.postal_code = value

    # Town candidate selection
    if not city_candidates:
        result.warnings.append("libpostal_no_city_label")
    elif len(city_candidates) == 1:
        result.town_candidate = city_candidates[0]
    else:
        # Multiple city-like labels: prefer the explicit "city" label
        # Fall back to the first candidate
        for value, label in components:
            if label == "city" and value.strip():
                result.town_candidate = value.strip()
                break
        if result.town_candidate is None:
            result.town_candidate = city_candidates[0]
        result.warnings.append("multiple_town_candidates")

    return result
