"""
Load and index the GeoNames cities5000.txt gazetteer.

Builds an in-memory, country-indexed dictionary for O(1) lookups.
Each country maps to a dict of normalized_name -> list[CityRecord].
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .preprocess import normalize_for_matching

logger = logging.getLogger(__name__)

# cities5000.txt TSV column indices (no header row)
COL_GEONAMEID = 0
COL_NAME = 1
COL_ASCIINAME = 2
COL_ALTERNATENAMES = 3
COL_LATITUDE = 4
COL_LONGITUDE = 5
COL_FEATURE_CLASS = 6
COL_FEATURE_CODE = 7
COL_COUNTRY_CODE = 8
COL_CC2 = 9
COL_ADMIN1 = 10
COL_ADMIN2 = 11
COL_POPULATION = 14
COL_TIMEZONE = 17
COL_MODIFICATION_DATE = 18


@dataclass
class CityRecord:
    """A single GeoNames city entry."""

    geonames_id: int
    name: str
    ascii_name: str
    alternate_names: list[str]
    country_code: str
    admin1_code: str
    population: int
    latitude: float
    longitude: float


@dataclass
class GeoNamesIndex:
    """
    Country-indexed lookup structure.

    Structure:
        country_code -> normalized_name -> list[CityRecord]

    A name can map to multiple cities (e.g. "springfield" in "US").
    """

    # Main lookup: country_code -> norm_name -> [CityRecord, ...]
    by_country: dict[str, dict[str, list[CityRecord]]] = field(default_factory=dict)

    # All normalized names per country (for fuzzy scan)
    country_names: dict[str, set[str]] = field(default_factory=dict)

    # Total records loaded
    total_records: int = 0

    def get_cities(self, country_code: str, normalized_name: str) -> list[CityRecord]:
        """Exact lookup by country and normalized name."""
        country = self.by_country.get(country_code.upper(), {})
        return country.get(normalized_name, [])

    def get_all_names(self, country_code: str) -> set[str]:
        """Return all normalized names for a country (for scan/fuzzy matching)."""
        return self.country_names.get(country_code.upper(), set())

    def has_country(self, country_code: str) -> bool:
        return country_code.upper() in self.by_country


def _register_name(
    index: GeoNamesIndex,
    country_code: str,
    raw_name: str,
    city: CityRecord,
) -> None:
    """Register a single name variant in the index."""
    norm = normalize_for_matching(raw_name)
    if not norm:
        return

    cc = country_code.upper()

    if cc not in index.by_country:
        index.by_country[cc] = {}
        index.country_names[cc] = set()

    index.country_names[cc].add(norm)

    if norm not in index.by_country[cc]:
        index.by_country[cc][norm] = []
    index.by_country[cc][norm].append(city)


def load_geonames(filepath: str | Path) -> GeoNamesIndex:
    """
    Parse cities5000.txt and build the country-indexed lookup.

    Args:
        filepath: Path to the GeoNames TSV file.

    Returns:
        Populated GeoNamesIndex ready for lookups.
    """
    index = GeoNamesIndex()
    filepath = Path(filepath)

    if not filepath.exists():
        raise FileNotFoundError(f"GeoNames file not found: {filepath}")

    logger.info("Loading GeoNames from %s ...", filepath)

    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        for row in reader:
            if len(row) < 19:
                continue  # skip malformed rows

            try:
                geonames_id = int(row[COL_GEONAMEID])
                population = int(row[COL_POPULATION]) if row[COL_POPULATION] else 0
                latitude = float(row[COL_LATITUDE]) if row[COL_LATITUDE] else 0.0
                longitude = float(row[COL_LONGITUDE]) if row[COL_LONGITUDE] else 0.0
            except (ValueError, IndexError):
                continue

            name = row[COL_NAME].strip()
            ascii_name = row[COL_ASCIINAME].strip()
            country_code = row[COL_COUNTRY_CODE].strip().upper()
            admin1_code = row[COL_ADMIN1].strip() if len(row) > COL_ADMIN1 else ""

            alt_names_raw = row[COL_ALTERNATENAMES].strip()
            alternate_names = (
                [n.strip() for n in alt_names_raw.split(",") if n.strip()]
                if alt_names_raw
                else []
            )

            city = CityRecord(
                geonames_id=geonames_id,
                name=name,
                ascii_name=ascii_name,
                alternate_names=alternate_names,
                country_code=country_code,
                admin1_code=admin1_code,
                population=population,
                latitude=latitude,
                longitude=longitude,
            )

            # Register primary name
            _register_name(index, country_code, name, city)

            # Register ASCII name (if different)
            if ascii_name and ascii_name.lower() != name.lower():
                _register_name(index, country_code, ascii_name, city)

            # Register all alternate names
            for alt in alternate_names:
                _register_name(index, country_code, alt, city)

            index.total_records += 1

    logger.info(
        "GeoNames loaded: %d records, %d countries",
        index.total_records,
        len(index.by_country),
    )
    return index
