"""
GeoNames ETL — Load raw GeoNames files into SQLite (dev) or PostgreSQL (prod).

Usage:
    python -m src.geonames_etl                      # defaults
    python -m src.geonames_etl --db data/geonames.db # custom path

Creates three tables:
    - geonames_cities       (~166k rows from cities1000.txt)
    - geonames_city_names   (~800k+ rows — normalized primary, ascii, alternate names)
    - geonames_postal_codes (~1.8M rows from allCountries.txt)
    - geonames_admin1       (~3.8k rows from admin1CodesASCII.txt)

Also creates indexes for exact lookup, prefix matching, and (SQLite) trigram-like
search. PostgreSQL users should swap the trigram index for a GIN pg_trgm index.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import sqlite3
import sys
import time
from pathlib import Path

from .preprocess import normalize_for_matching

logger = logging.getLogger(__name__)

# ── Default paths ─────────────────────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "database" / "geonames.db"

CITIES_FILE = DATA_DIR / "cities500.txt"
POSTAL_FILE = DATA_DIR / "allCountries.txt"
ADMIN1_FILE = DATA_DIR / "admin1CodesASCII.txt"

# ── cities500.txt column indices (no header) ──────────────────────────
C_GEONAMEID = 0
C_NAME = 1
C_ASCIINAME = 2
C_ALTERNATENAMES = 3
C_LATITUDE = 4
C_LONGITUDE = 5
C_FEATURE_CLASS = 6
C_FEATURE_CODE = 7
C_COUNTRY_CODE = 8
C_ADMIN1 = 10
C_POPULATION = 14

# ── allCountries.txt (postal codes) column indices ────────────────────
P_COUNTRY_CODE = 0
P_POSTAL_CODE = 1
P_PLACE_NAME = 2
P_ADMIN_NAME1 = 3
P_ADMIN_CODE1 = 4
P_ADMIN_NAME2 = 5
P_ADMIN_CODE2 = 6
P_ADMIN_NAME3 = 7
P_ADMIN_CODE3 = 8
P_LATITUDE = 9
P_LONGITUDE = 10
P_ACCURACY = 11

# ── admin1CodesASCII.txt column indices ───────────────────────────────
A_CODE = 0       # e.g. "US.IL"
A_NAME = 1       # "Illinois"
A_ASCII_NAME = 2  # "Illinois"
A_GEONAMEID = 3

# ── Batch size for inserts ────────────────────────────────────────────
BATCH_SIZE = 10_000


# =====================================================================
#  Schema
# =====================================================================

SCHEMA_SQL = """
-- ── Cities ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS geonames_cities (
    geonameid       INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL,
    ascii_name      TEXT,
    alternate_names TEXT,           -- comma-separated raw string
    country_code    TEXT    NOT NULL,
    admin1_code     TEXT,
    population      INTEGER DEFAULT 0,
    latitude        REAL,
    longitude       REAL,
    feature_code    TEXT
);

-- ── Normalized city names (for exact + fuzzy lookup) ─────────────────
CREATE TABLE IF NOT EXISTS geonames_city_names (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    geonameid       INTEGER NOT NULL REFERENCES geonames_cities(geonameid),
    normalized_name TEXT    NOT NULL,
    name_type       TEXT    NOT NULL,   -- 'primary', 'ascii', 'alternate'
    country_code    TEXT    NOT NULL
);

-- ── Postal codes ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS geonames_postal_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    country_code    TEXT    NOT NULL,
    postal_code     TEXT    NOT NULL,
    place_name      TEXT,
    admin_name1     TEXT,
    admin_code1     TEXT,
    latitude        REAL,
    longitude       REAL,
    accuracy        INTEGER
);

-- ── Admin1 regions ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS geonames_admin1 (
    code            TEXT    PRIMARY KEY,  -- e.g. "US.IL"
    name            TEXT    NOT NULL,
    ascii_name      TEXT,
    geonameid       INTEGER
);

-- ── Version tracking ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS geonames_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset         TEXT    NOT NULL,    -- 'cities', 'postal_codes', 'admin1'
    record_count    INTEGER NOT NULL,
    file_hash       TEXT,               -- SHA-256 of source file
    loaded_at       TEXT    DEFAULT (datetime('now')),
    is_active       INTEGER DEFAULT 1
);
"""

INDEX_SQL = """
-- Cities indexes
CREATE INDEX IF NOT EXISTS idx_cities_cc        ON geonames_cities (country_code);
CREATE INDEX IF NOT EXISTS idx_cities_cc_admin1  ON geonames_cities (country_code, admin1_code);

-- City names indexes (core lookup)
CREATE INDEX IF NOT EXISTS idx_citynames_lookup  ON geonames_city_names (country_code, normalized_name);
CREATE INDEX IF NOT EXISTS idx_citynames_geoname ON geonames_city_names (geonameid);

-- Postal code indexes
CREATE INDEX IF NOT EXISTS idx_postal_cc_code    ON geonames_postal_codes (country_code, postal_code);
CREATE INDEX IF NOT EXISTS idx_postal_cc_prefix  ON geonames_postal_codes (country_code, substr(postal_code, 1, 3));
CREATE INDEX IF NOT EXISTS idx_postal_place      ON geonames_postal_codes (country_code, place_name);

-- Admin1 indexes
CREATE INDEX IF NOT EXISTS idx_admin1_cc         ON geonames_admin1 (substr(code, 1, 2));
"""


# =====================================================================
#  Helpers
# =====================================================================

def _sha256(filepath: Path) -> str:
    """Compute SHA-256 hash of a file (read in chunks)."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_int(val: str, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val: str, default: float | None = None) -> float | None:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# =====================================================================
#  ETL Functions
# =====================================================================

def load_cities(conn: sqlite3.Connection, filepath: Path) -> int:
    """Load cities1000.txt into geonames_cities + geonames_city_names."""
    logger.info("Loading cities from %s ...", filepath)
    cur = conn.cursor()

    city_batch: list[tuple] = []
    name_batch: list[tuple] = []
    count = 0
    name_count = 0

    with open(filepath, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        for row in reader:
            if len(row) < 15:
                continue

            geonameid = _safe_int(row[C_GEONAMEID])
            name = row[C_NAME].strip()
            ascii_name = row[C_ASCIINAME].strip()
            alt_names_raw = row[C_ALTERNATENAMES].strip()
            cc = row[C_COUNTRY_CODE].strip().upper()
            admin1 = row[C_ADMIN1].strip()
            population = _safe_int(row[C_POPULATION])
            lat = _safe_float(row[C_LATITUDE])
            lon = _safe_float(row[C_LONGITUDE])
            feature_code = row[C_FEATURE_CODE].strip() if len(row) > C_FEATURE_CODE else ""

            city_batch.append((
                geonameid, name, ascii_name, alt_names_raw,
                cc, admin1, population, lat, lon, feature_code,
            ))

            # Build normalized name variants
            seen_norms: set[str] = set()

            # Primary name
            norm_primary = normalize_for_matching(name)
            if norm_primary:
                name_batch.append((geonameid, norm_primary, "primary", cc))
                seen_norms.add(norm_primary)
                name_count += 1

            # ASCII name (if different)
            if ascii_name:
                norm_ascii = normalize_for_matching(ascii_name)
                if norm_ascii and norm_ascii not in seen_norms:
                    name_batch.append((geonameid, norm_ascii, "ascii", cc))
                    seen_norms.add(norm_ascii)
                    name_count += 1

            # Alternate names (split by comma)
            if alt_names_raw:
                for alt in alt_names_raw.split(","):
                    alt = alt.strip()
                    if not alt:
                        continue
                    norm_alt = normalize_for_matching(alt)
                    if norm_alt and norm_alt not in seen_norms:
                        name_batch.append((geonameid, norm_alt, "alternate", cc))
                        seen_norms.add(norm_alt)
                        name_count += 1

            count += 1

            if count % BATCH_SIZE == 0:
                cur.executemany(
                    "INSERT OR REPLACE INTO geonames_cities VALUES (?,?,?,?,?,?,?,?,?,?)",
                    city_batch,
                )
                cur.executemany(
                    "INSERT INTO geonames_city_names (geonameid, normalized_name, name_type, country_code) VALUES (?,?,?,?)",
                    name_batch,
                )
                city_batch.clear()
                name_batch.clear()
                logger.info("  ... %d cities loaded", count)

    # Flush remaining
    if city_batch:
        cur.executemany(
            "INSERT OR REPLACE INTO geonames_cities VALUES (?,?,?,?,?,?,?,?,?,?)",
            city_batch,
        )
    if name_batch:
        cur.executemany(
            "INSERT INTO geonames_city_names (geonameid, normalized_name, name_type, country_code) VALUES (?,?,?,?)",
            name_batch,
        )

    conn.commit()

    # Record version
    file_hash = _sha256(filepath)
    cur.execute(
        "INSERT INTO geonames_versions (dataset, record_count, file_hash) VALUES (?, ?, ?)",
        ("cities", count, file_hash),
    )
    conn.commit()

    logger.info("✅ Loaded %d cities, %d name variants", count, name_count)
    return count


def load_postal_codes(conn: sqlite3.Connection, filepath: Path) -> int:
    """Load allCountries.txt (postal codes) into geonames_postal_codes."""
    logger.info("Loading postal codes from %s ...", filepath)
    cur = conn.cursor()

    batch: list[tuple] = []
    count = 0

    with open(filepath, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        for row in reader:
            if len(row) < 10:
                continue

            cc = row[P_COUNTRY_CODE].strip().upper()
            postal_code = row[P_POSTAL_CODE].strip()
            place_name = row[P_PLACE_NAME].strip()
            admin_name1 = row[P_ADMIN_NAME1].strip() if len(row) > P_ADMIN_NAME1 else ""
            admin_code1 = row[P_ADMIN_CODE1].strip() if len(row) > P_ADMIN_CODE1 else ""
            lat = _safe_float(row[P_LATITUDE]) if len(row) > P_LATITUDE else None
            lon = _safe_float(row[P_LONGITUDE]) if len(row) > P_LONGITUDE else None
            accuracy = _safe_int(row[P_ACCURACY]) if len(row) > P_ACCURACY else None

            batch.append((cc, postal_code, place_name, admin_name1, admin_code1, lat, lon, accuracy))
            count += 1

            if count % BATCH_SIZE == 0:
                cur.executemany(
                    "INSERT INTO geonames_postal_codes (country_code, postal_code, place_name, admin_name1, admin_code1, latitude, longitude, accuracy) VALUES (?,?,?,?,?,?,?,?)",
                    batch,
                )
                batch.clear()
                if count % 100_000 == 0:
                    logger.info("  ... %d postal codes loaded", count)

    if batch:
        cur.executemany(
            "INSERT INTO geonames_postal_codes (country_code, postal_code, place_name, admin_name1, admin_code1, latitude, longitude, accuracy) VALUES (?,?,?,?,?,?,?,?)",
            batch,
        )

    conn.commit()

    file_hash = _sha256(filepath)
    cur.execute(
        "INSERT INTO geonames_versions (dataset, record_count, file_hash) VALUES (?, ?, ?)",
        ("postal_codes", count, file_hash),
    )
    conn.commit()

    logger.info("✅ Loaded %d postal codes", count)
    return count


def load_admin1(conn: sqlite3.Connection, filepath: Path) -> int:
    """Load admin1CodesASCII.txt into geonames_admin1."""
    logger.info("Loading admin1 codes from %s ...", filepath)
    cur = conn.cursor()

    batch: list[tuple] = []
    count = 0

    with open(filepath, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        for row in reader:
            if len(row) < 4:
                continue

            code = row[A_CODE].strip()          # e.g. "US.IL"
            name = row[A_NAME].strip()
            ascii_name = row[A_ASCII_NAME].strip()
            geonameid = _safe_int(row[A_GEONAMEID])

            batch.append((code, name, ascii_name, geonameid))
            count += 1

    if batch:
        cur.executemany(
            "INSERT OR REPLACE INTO geonames_admin1 VALUES (?,?,?,?)",
            batch,
        )

    conn.commit()

    file_hash = _sha256(filepath)
    cur.execute(
        "INSERT INTO geonames_versions (dataset, record_count, file_hash) VALUES (?, ?, ?)",
        ("admin1", count, file_hash),
    )
    conn.commit()

    logger.info("✅ Loaded %d admin1 codes", count)
    return count


# =====================================================================
#  Main
# =====================================================================

def run_etl(db_path: Path | str = DEFAULT_DB) -> None:
    """Run the full GeoNames ETL into SQLite."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Remove existing DB for a clean load
    if db_path.exists():
        logger.info("Removing existing DB: %s", db_path)
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")      # faster bulk insert
    conn.execute("PRAGMA cache_size=-64000")     # 64MB cache
    conn.execute("PRAGMA temp_store=MEMORY")

    t0 = time.time()

    # Create schema
    logger.info("Creating schema ...")
    conn.executescript(SCHEMA_SQL)

    # Load data
    cities_count = 0
    postal_count = 0
    admin1_count = 0

    if CITIES_FILE.exists():
        cities_count = load_cities(conn, CITIES_FILE)
    else:
        logger.warning("⚠️  cities1000.txt not found at %s", CITIES_FILE)

    if ADMIN1_FILE.exists():
        admin1_count = load_admin1(conn, ADMIN1_FILE)
    else:
        logger.warning("⚠️  admin1CodesASCII.txt not found at %s", ADMIN1_FILE)

    if POSTAL_FILE.exists():
        postal_count = load_postal_codes(conn, POSTAL_FILE)
    else:
        logger.warning("⚠️  allCountries.txt not found at %s", POSTAL_FILE)

    # Create indexes (after bulk insert for speed)
    logger.info("Creating indexes ...")
    conn.executescript(INDEX_SQL)

    # Optimize
    logger.info("Running ANALYZE ...")
    conn.execute("ANALYZE")
    conn.execute("PRAGMA optimize")

    elapsed = time.time() - t0
    db_size_mb = db_path.stat().st_size / (1024 * 1024)

    logger.info("=" * 60)
    logger.info("ETL Complete in %.1fs", elapsed)
    logger.info("  Cities:       %d", cities_count)
    logger.info("  City names:   (see geonames_city_names table)")
    logger.info("  Admin1:       %d", admin1_count)
    logger.info("  Postal codes: %d", postal_count)
    logger.info("  DB size:      %.1f MB", db_size_mb)
    logger.info("  DB path:      %s", db_path)
    logger.info("=" * 60)

    conn.close()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="GeoNames ETL → SQLite")
    parser.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_DB),
        help=f"Path to SQLite database (default: {DEFAULT_DB})",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    run_etl(db_path=args.db)


if __name__ == "__main__":
    main()
