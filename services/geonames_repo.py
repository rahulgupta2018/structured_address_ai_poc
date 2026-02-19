"""
GeoNames SQLite repository — shared data access layer.

All GeoNames queries go through this module. Services call these functions
rather than accessing SQLite directly. The 5 query functions at the bottom
double as ADK LLM tools (registered in sub_agents/llm_parser/tools.py).

Connection management: module-level singleton, lazily initialised, with
atexit cleanup.
"""

from __future__ import annotations

import atexit
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz, process

from utils.config import GEONAMES_DB_PATH

logger = logging.getLogger(__name__)

# ── Connection singleton ─────────────────────────────────────────────────────

_connection: Optional[sqlite3.Connection] = None


def get_connection(db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    """Return a shared read-only SQLite connection (lazily created).

    Args:
        db_path: Override path (mainly for testing). Defaults to
                 ``utils.config.GEONAMES_DB_PATH``.
    """
    global _connection
    if _connection is not None:
        return _connection

    path = str(db_path or GEONAMES_DB_PATH)
    logger.info("Opening GeoNames SQLite database: %s", path)
    _connection = sqlite3.connect(path, check_same_thread=False)
    _connection.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrent read performance
    _connection.execute("PRAGMA journal_mode=WAL")
    _connection.execute("PRAGMA query_only=ON")
    return _connection


def close_connection() -> None:
    """Close the shared connection (called at interpreter shutdown)."""
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None
        logger.debug("GeoNames SQLite connection closed")


atexit.register(close_connection)


def reset_connection() -> None:
    """Force-close and re-open on next access (useful for tests)."""
    close_connection()


# ── Internal helpers ─────────────────────────────────────────────────────────


def _rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    """Convert sqlite3.Row results to plain dicts."""
    return [dict(row) for row in cursor.fetchall()]


# ── Core query functions (also used as LLM tools) ───────────────────────────


def query_city(city_name: str, country_code: str) -> list[dict]:
    """Search GeoNames for a city by name within a country.

    Looks up the normalized name in the city_names index, then joins
    back to the cities table for full details. Returns matches sorted
    by population (largest first).

    Args:
        city_name: City name to search for (case-insensitive).
        country_code: ISO 3166-1 alpha-2 country code.

    Returns:
        List of matching city dicts with keys: geonameid, name,
        ascii_name, country_code, admin1_code, population, name_type.
    """
    conn = get_connection()
    normalized = city_name.strip().lower()
    cc = country_code.strip().upper()

    cursor = conn.execute(
        """
        SELECT c.geonameid, c.name, c.ascii_name, c.country_code,
               c.admin1_code, c.population, n.name_type
        FROM geonames_city_names n
        JOIN geonames_cities c ON n.geonameid = c.geonameid
        WHERE n.country_code = ? AND n.normalized_name = ?
        ORDER BY c.population DESC
        """,
        (cc, normalized),
    )
    rows = _rows_to_dicts(cursor)
    # When the match is via an alternate name, include the queried name
    # so the LLM can preserve the address spelling (e.g. "Morbi" vs "Morvi").
    for row in rows:
        if row.get("name_type") == "alternate":
            row["matched_alternate_name"] = city_name.strip()
    return rows


def list_countries_for_city(city_name: str) -> list[dict]:
    """Find all countries that contain a city with this name.

    Useful for detecting country-code mismatches (e.g., "Barisardo"
    exists in IT but the input says IE).

    Args:
        city_name: City name to search for (case-insensitive).

    Returns:
        List of dicts with keys: country_code, geonameid, name,
        population, name_type.
    """
    conn = get_connection()
    normalized = city_name.strip().lower()

    cursor = conn.execute(
        """
        SELECT DISTINCT c.country_code, c.geonameid, c.name,
               c.population, n.name_type
        FROM geonames_city_names n
        JOIN geonames_cities c ON n.geonameid = c.geonameid
        WHERE n.normalized_name = ?
        ORDER BY c.population DESC
        """,
        (normalized,),
    )
    rows = _rows_to_dicts(cursor)
    for row in rows:
        if row.get("name_type") == "alternate":
            row["matched_alternate_name"] = city_name.strip()
    return rows


def query_postal_code(postal_code: str, country_code: str) -> list[dict]:
    """Look up places associated with a postal code in a country.

    Args:
        postal_code: The postal/ZIP code string.
        country_code: ISO 3166-1 alpha-2 country code.

    Returns:
        List of dicts with keys: postal_code, place_name, admin_name1,
        admin_code1, country_code, latitude, longitude.
    """
    conn = get_connection()
    code = postal_code.strip()
    cc = country_code.strip().upper()

    cursor = conn.execute(
        """
        SELECT postal_code, place_name, admin_name1, admin_code1,
               country_code, latitude, longitude
        FROM geonames_postal_codes
        WHERE country_code = ? AND postal_code = ?
        ORDER BY place_name
        """,
        (cc, code),
    )
    return _rows_to_dicts(cursor)


def search_postal_by_place_name(town_name: str) -> list[dict]:
    """Search the postal-codes table by place name (all countries).

    Useful for towns too small for the cities1000 dataset but present
    in the postal-codes data (e.g. Barisardo → 'Bari Sardo' in IT).

    Tries exact normalized match first, then a space-insensitive LIKE
    pattern so that 'barisardo' matches 'Bari Sardo'.

    Returns:
        List of dicts with keys: postal_code, place_name, admin_name1,
        admin_code1, country_code, latitude, longitude.
    """
    conn = get_connection()
    norm = town_name.strip().lower()
    if not norm:
        return []

    # 1) Exact (case-insensitive) match on place_name
    cursor = conn.execute(
        """
        SELECT postal_code, place_name, admin_name1, admin_code1,
               country_code, latitude, longitude
        FROM geonames_postal_codes
        WHERE LOWER(place_name) = ?
        ORDER BY place_name
        LIMIT 10
        """,
        (norm,),
    )
    rows = _rows_to_dicts(cursor)
    if rows:
        return rows

    # 2) Space-insensitive: strip spaces from both sides and compare
    #    e.g. 'barisardo' matches 'Bari Sardo' (stored as 'bari sardo')
    squashed = norm.replace(" ", "").replace("-", "")
    if len(squashed) < 4:
        return []

    cursor = conn.execute(
        """
        SELECT postal_code, place_name, admin_name1, admin_code1,
               country_code, latitude, longitude
        FROM geonames_postal_codes
        WHERE REPLACE(REPLACE(LOWER(place_name), ' ', ''), '-', '') = ?
        ORDER BY place_name
        LIMIT 10
        """,
        (squashed,),
    )
    return _rows_to_dicts(cursor)


def query_city_fuzzy(
    city_name: str, country_code: str, threshold: int = 80
) -> list[dict]:
    """Fuzzy-match a city name in GeoNames (uses edit distance).

    Retrieves all normalized names for the country, then uses
    rapidfuzz to find the best matches above the threshold.

    Args:
        city_name: Approximate city name.
        country_code: ISO 3166-1 alpha-2 country code.
        threshold: Minimum fuzzy score (0–100). Default 80.

    Returns:
        Top-5 fuzzy matches as dicts with keys: normalized_name, score,
        geonameid, name, population.
    """
    conn = get_connection()
    normalized = city_name.strip().lower()
    cc = country_code.strip().upper()

    # Get all unique normalized names for this country
    cursor = conn.execute(
        """
        SELECT DISTINCT normalized_name
        FROM geonames_city_names
        WHERE country_code = ?
        """,
        (cc,),
    )
    all_names = [row["normalized_name"] for row in cursor.fetchall()]

    if not all_names:
        return []

    # Run rapidfuzz
    results = process.extract(
        normalized, all_names, scorer=fuzz.ratio, limit=5
    )

    matches = []
    for name, score, _ in results:
        if score < threshold:
            continue
        # Look up city details for this name
        city_cursor = conn.execute(
            """
            SELECT c.geonameid, c.name, c.population
            FROM geonames_city_names n
            JOIN geonames_cities c ON n.geonameid = c.geonameid
            WHERE n.country_code = ? AND n.normalized_name = ?
            ORDER BY c.population DESC
            LIMIT 1
            """,
            (cc, name),
        )
        city_row = city_cursor.fetchone()
        if city_row:
            matches.append({
                "normalized_name": name,
                "score": round(score, 1),
                "geonameid": city_row["geonameid"],
                "name": city_row["name"],
                "population": city_row["population"],
            })

    return matches


def query_city_by_admin1(
    city_name: str, admin1_name: str, country_code: str
) -> list[dict]:
    """Search for a city within a specific admin1 region (state/province).

    Joins the admin1 table to resolve the admin1 name to a code, then
    filters cities by that code.

    Args:
        city_name: City name (case-insensitive).
        admin1_name: State/province/region name (case-insensitive).
        country_code: ISO 3166-1 alpha-2 country code.

    Returns:
        List of matching city dicts with keys: geonameid, name,
        admin1_code, admin1_name, population.
    """
    conn = get_connection()
    normalized = city_name.strip().lower()
    cc = country_code.strip().upper()
    admin1_lower = admin1_name.strip().lower()

    # Resolve admin1 name → code
    admin_cursor = conn.execute(
        """
        SELECT code, name
        FROM geonames_admin1
        WHERE code LIKE ? AND LOWER(name) = ?
        """,
        (f"{cc}.%", admin1_lower),
    )
    admin_row = admin_cursor.fetchone()

    if not admin_row:
        return []

    # Extract admin1 code (the part after "CC.")
    admin1_code = admin_row["code"].split(".")[-1] if admin_row else ""

    cursor = conn.execute(
        """
        SELECT c.geonameid, c.name, c.admin1_code, c.population
        FROM geonames_city_names n
        JOIN geonames_cities c ON n.geonameid = c.geonameid
        WHERE n.country_code = ? AND n.normalized_name = ?
              AND c.admin1_code = ?
        ORDER BY c.population DESC
        """,
        (cc, normalized, admin1_code),
    )

    results = _rows_to_dicts(cursor)
    for r in results:
        r["admin1_name"] = admin_row["name"]
    return results


# ── Convenience queries used by deterministic services ───────────────────────


def get_all_normalized_names(country_code: str) -> set[str]:
    """Return all normalized city names for a country (for scan matching).

    Args:
        country_code: ISO 3166-1 alpha-2 country code.

    Returns:
        Set of normalized name strings.
    """
    conn = get_connection()
    cc = country_code.strip().upper()

    cursor = conn.execute(
        """
        SELECT DISTINCT normalized_name
        FROM geonames_city_names
        WHERE country_code = ?
        """,
        (cc,),
    )
    return {row["normalized_name"] for row in cursor.fetchall()}


def resolve_city_by_name(
    country_code: str, normalized_name: str
) -> Optional[dict]:
    """Resolve a normalized name to the best (highest-population) city.

    Returns None if no match. Used by address_scanner and geonames_exact.
    """
    conn = get_connection()
    cc = country_code.strip().upper()

    cursor = conn.execute(
        """
        SELECT c.geonameid, c.name, c.ascii_name, c.country_code,
               c.admin1_code, c.population, n.name_type
        FROM geonames_city_names n
        JOIN geonames_cities c ON n.geonameid = c.geonameid
        WHERE n.country_code = ? AND n.normalized_name = ?
        ORDER BY c.population DESC
        LIMIT 1
        """,
        (cc, normalized_name),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def resolve_all_cities_by_name(
    country_code: str, normalized_name: str
) -> list[dict]:
    """Resolve a normalized name to ALL matching cities (sorted by population).

    Unlike ``resolve_city_by_name`` which returns only the largest city,
    this returns every match — enabling callers to disambiguate using
    postal code, admin1, or other signals.

    Args:
        country_code: ISO 3166-1 alpha-2 country code.
        normalized_name: Lowercased city name.

    Returns:
        List of city dicts (may be empty). Each dict has keys:
        geonameid, name, ascii_name, country_code, admin1_code,
        population, name_type.
    """
    conn = get_connection()
    cc = country_code.strip().upper()

    cursor = conn.execute(
        """
        SELECT c.geonameid, c.name, c.ascii_name, c.country_code,
               c.admin1_code, c.population, n.name_type
        FROM geonames_city_names n
        JOIN geonames_cities c ON n.geonameid = c.geonameid
        WHERE n.country_code = ? AND n.normalized_name = ?
        ORDER BY c.population DESC
        """,
        (cc, normalized_name),
    )
    return _rows_to_dicts(cursor)
