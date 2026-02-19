"""
File reader — reads Excel (.xlsx/.xls) and CSV (.csv) input files.

Returns:
    list[dict] — one dict per row with keys:
        address_1, address_2, address_3, country_code
        (plus any extra columns from the source file)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Common column-name variants → canonical names
_ALIASES = {
    "addr_1": "address_1",
    "addr_2": "address_2",
    "addr_3": "address_3",
    "addr1": "address_1",
    "addr2": "address_2",
    "addr3": "address_3",
    "address1": "address_1",
    "address2": "address_2",
    "address3": "address_3",
    "line_1": "address_1",
    "line_2": "address_2",
    "line_3": "address_3",
    "address_line_1": "address_1",
    "address_line_2": "address_2",
    "address_line_3": "address_3",
    "cc": "country_code",
    "country": "country_code",
}


def read_input(filepath: str | Path) -> list[dict]:
    """Read an Excel or CSV file and return a list of row dicts.

    Column names are normalised to lowercase/underscore and aliased
    to canonical names. ``keep_default_na=False`` prevents pandas from
    converting ISO country codes like "NA" (Namibia) to NaN.

    Args:
        filepath: Path to the input file (.xlsx, .xls, or .csv).

    Returns:
        List of dicts, one per valid row. Invalid rows are logged and
        skipped.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the format is unsupported or country_code column
            is missing.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Input file not found: {filepath}")

    ext = filepath.suffix.lower()

    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(filepath, dtype=str, keep_default_na=False)
    elif ext == ".csv":
        df = pd.read_csv(filepath, dtype=str, keep_default_na=False)
    else:
        raise ValueError(
            f"Unsupported file format '{ext}'. "
            f"Expected .xlsx, .xls, or .csv ({filepath.name})"
        )

    logger.info("Reading input file: %s (%d rows)", filepath, len(df))

    # Normalise column names
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    df.columns = [_ALIASES.get(c, c) for c in df.columns]

    if "country_code" not in df.columns:
        raise ValueError(
            "Input file must contain a 'country_code' (or equivalent) column. "
            f"Found columns: {list(df.columns)}"
        )

    rows: list[dict] = []
    for idx, row_data in df.iterrows():
        cc = str(row_data.get("country_code", "")).strip()
        if len(cc) < 2:
            logger.warning("Skipping row %d: invalid country_code '%s'", idx, cc)
            continue

        row = {
            "address_1": _clean(row_data.get("address_1")),
            "address_2": _clean(row_data.get("address_2")),
            "address_3": _clean(row_data.get("address_3")),
            "country_code": cc.upper(),
        }
        rows.append(row)

    logger.info("Loaded %d valid rows from %d total", len(rows), len(df))
    return rows


def _clean(value: Optional[object]) -> Optional[str]:
    """Coerce a cell value to a clean string or None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s if s else None
