"""
File writer — writes pipeline results to Excel (.xlsx) or CSV (.csv).

Accepts a list of result dicts (from PersistAgent's ``state["final_result"]``)
and writes them to the specified output path. Format is auto-detected from the
file extension.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# Canonical output column order
_OUTPUT_COLUMNS = [
    "input_address_1",
    "input_address_2",
    "input_address_3",
    "country_code",
    "address_line_1",
    "address_line_2",
    "building",
    "street",
    "town",
    "country",
    "postal_code",
    "status",
    "confidence_score",
    "parser_source",
    "geonames_match",
    "geonames_id",
    "normalized_town",
    "warnings",
    "review_reason",
    "mismatch_detected",
    "suggested_country_code",
]


def write_output(
    results: list[dict],
    filepath: str | Path,
    sheet_name: str = "Results",
) -> Path:
    """Write pipeline results to an Excel or CSV file.

    Args:
        results: List of result dicts (one per row).
        filepath: Output file path (.xlsx or .csv).
        sheet_name: Worksheet name (Excel only).

    Returns:
        Resolved Path of the written file.
    """
    filepath = Path(filepath).resolve()
    filepath.parent.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(results)

    # Reorder to canonical column order
    ordered = [c for c in _OUTPUT_COLUMNS if c in df.columns]
    extra = [c for c in df.columns if c not in _OUTPUT_COLUMNS]
    df = df[ordered + extra]

    ext = filepath.suffix.lower()

    if ext == ".csv":
        df.to_csv(filepath, index=False)
    else:
        # Default to Excel
        df.to_excel(filepath, sheet_name=sheet_name, index=False)

    logger.info("Output written to %s (%d rows)", filepath, len(results))
    return filepath
