"""
Excel I/O helpers.

Reads input addresses from an Excel workbook and writes the fully
populated output back to a new workbook.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from .schemas import AddressInput, AddressOutput

logger = logging.getLogger(__name__)

# ── Column mappings ──────────────────────────────────────────────────────────

_INPUT_COLUMNS = ["address_1", "address_2", "address_3", "country_code"]

_OUTPUT_COLUMNS = [
    # Original input (audit trail)
    "address_1",
    "address_2",
    "address_3",
    "country_code",
    # Extracted fields
    "town",
    "street",
    "building",
    "postal_code",
    # Pipeline metadata
    "status",
    "confidence_score",
    "parser_source",
    "geonames_match",
    "geonames_id",
    "normalized_town",
    "warnings",
    "review_reason",
]


def read_input(filepath: str | Path) -> list[AddressInput]:
    """
    Read an Excel file and return a list of validated AddressInput rows.

    Columns are matched by name (case-insensitive). Missing optional
    columns are treated as null.

    Args:
        filepath: Path to the input .xlsx file.

    Returns:
        List of AddressInput objects. Invalid rows are logged and skipped.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Input file not found: {filepath}")

    logger.info("Reading input file: %s", filepath)
    df = pd.read_excel(filepath, dtype=str)

    # Normalize column names to lowercase / underscore
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    # Map common column-name variants to canonical names
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
    df.columns = [_ALIASES.get(c, c) for c in df.columns]

    # Validate that country_code column exists
    if "country_code" not in df.columns:
        raise ValueError(
            "Input file must contain a 'country_code' (or equivalent) column. "
            f"Found columns: {list(df.columns)}"
        )

    rows: list[AddressInput] = []
    for idx, row_data in df.iterrows():
        try:
            inp = AddressInput(
                address_1=_clean(row_data.get("address_1")),
                address_2=_clean(row_data.get("address_2")),
                address_3=_clean(row_data.get("address_3")),
                country_code=str(row_data.get("country_code", "")).strip(),
            )
            rows.append(inp)
        except Exception as e:
            logger.warning("Skipping row %d: %s", idx, e)

    logger.info("Loaded %d valid rows from %d total", len(rows), len(df))
    return rows


def write_output(
    results: list[AddressOutput],
    filepath: str | Path,
    sheet_name: str = "Results",
) -> Path:
    """
    Write pipeline results to an Excel workbook.

    Args:
        results:    List of AddressOutput objects.
        filepath:   Path for the output .xlsx file.
        sheet_name: Worksheet name.

    Returns:
        Resolved Path of the written file.
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    records = []
    for r in results:
        record = r.model_dump()
        # Flatten warnings list to a semicolon-separated string for Excel
        record["warnings"] = "; ".join(record.get("warnings", []))
        # Convert enums to their string values
        if record.get("status"):
            record["status"] = record["status"].value if hasattr(record["status"], "value") else str(record["status"])
        if record.get("parser_source"):
            record["parser_source"] = (
                record["parser_source"].value
                if hasattr(record["parser_source"], "value")
                else str(record["parser_source"])
            )
        records.append(record)

    df = pd.DataFrame(records)

    # Reorder to the canonical output column order
    ordered_cols = [c for c in _OUTPUT_COLUMNS if c in df.columns]
    extra_cols = [c for c in df.columns if c not in _OUTPUT_COLUMNS]
    df = df[ordered_cols + extra_cols]

    df.to_excel(filepath, sheet_name=sheet_name, index=False)
    logger.info("Output written to %s (%d rows)", filepath, len(results))
    return filepath


def _clean(value: Optional[object]) -> Optional[str]:
    """Coerce a cell value to a clean string or None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s if s else None
