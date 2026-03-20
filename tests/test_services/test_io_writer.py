"""Tests for io_writer service — file writing."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from services.io_writer import _OUTPUT_COLUMNS, write_output

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with your own data               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

SAMPLE_ADDRESS_1 = "123 Main St"
SAMPLE_ADDRESS_2 = "Apt 4"
SAMPLE_ADDRESS_3 = "Springfield"
SAMPLE_COUNTRY_CODE = "US"
SAMPLE_TOWN = "Springfield"
SAMPLE_STREET = "Main St"
SAMPLE_BUILDING = "123"
SAMPLE_POSTAL_CODE = "62701"
SAMPLE_GEONAMES_ID = 4250542


# ── Fixtures ─────────────────────────────────────────────────────────────────

SAMPLE_RESULTS = [
    {
        "input_address_1": SAMPLE_ADDRESS_1,
        "input_address_2": SAMPLE_ADDRESS_2,
        "input_address_3": SAMPLE_ADDRESS_3,
        "country_code": SAMPLE_COUNTRY_CODE,
        "address_line_1": "123, Main St, 62701",
        "address_line_2": "",
        "town": SAMPLE_TOWN,
        "country": "United States",
        "street": SAMPLE_STREET,
        "building": SAMPLE_BUILDING,
        "postal_code": SAMPLE_POSTAL_CODE,
        "status": "validated",
        "confidence_score": 1.0,
        "parser_source": "libpostal",
        "geonames_match": True,
        "geonames_id": SAMPLE_GEONAMES_ID,
        "normalized_town": SAMPLE_TOWN,
        "warnings": "",
        "review_reason": None,
        "mismatch_detected": False,
        "suggested_country_code": None,
    },
]


class TestWriteCSV:
    def test_writes_csv(self, tmp_path):
        out_file = tmp_path / "output.csv"
        result_path = write_output(SAMPLE_RESULTS, out_file)

        assert result_path.exists()
        df = pd.read_csv(result_path)
        report("write_output CSV", {"file": str(out_file.name), "rows": len(df), "columns": list(df.columns), "town": df.iloc[0]["town"]})
        assert len(df) == 1
        assert df.iloc[0]["town"] == SAMPLE_TOWN
        assert df.iloc[0]["country_code"] == SAMPLE_COUNTRY_CODE

    def test_csv_column_order(self, tmp_path):
        out_file = tmp_path / "output.csv"
        write_output(SAMPLE_RESULTS, out_file)

        df = pd.read_csv(out_file)
        # First columns should follow _OUTPUT_COLUMNS order
        expected_first = [c for c in _OUTPUT_COLUMNS if c in df.columns]
        assert list(df.columns[: len(expected_first)]) == expected_first


class TestWriteExcel:
    def test_writes_xlsx(self, tmp_path):
        out_file = tmp_path / "output.xlsx"
        result_path = write_output(SAMPLE_RESULTS, out_file)

        assert result_path.exists()
        df = pd.read_excel(result_path)
        report("write_output XLSX", {"file": str(out_file.name), "rows": len(df), "town": df.iloc[0]["town"]})
        assert len(df) == 1
        assert df.iloc[0]["town"] == SAMPLE_TOWN

    def test_custom_sheet_name(self, tmp_path):
        out_file = tmp_path / "output.xlsx"
        write_output(SAMPLE_RESULTS, out_file, sheet_name="MySheet")

        df = pd.read_excel(out_file, sheet_name="MySheet")
        assert len(df) == 1


class TestWriteOutputEdgeCases:
    def test_creates_parent_directories(self, tmp_path):
        out_file = tmp_path / "sub" / "dir" / "output.csv"
        result_path = write_output(SAMPLE_RESULTS, out_file)

        assert result_path.exists()

    def test_empty_results(self, tmp_path):
        out_file = tmp_path / "empty.csv"
        result_path = write_output([], out_file)

        assert result_path.exists()
        # Empty DataFrame produces a file with no data rows
        assert result_path.stat().st_size <= 1  # may contain a trailing newline

    def test_extra_columns_appended(self, tmp_path):
        """Columns not in _OUTPUT_COLUMNS should appear at the end."""
        results = [{**SAMPLE_RESULTS[0], "custom_field": "extra"}]
        out_file = tmp_path / "output.csv"
        write_output(results, out_file)

        df = pd.read_csv(out_file)
        assert "custom_field" in df.columns
        assert df.iloc[0]["custom_field"] == "extra"

    def test_returns_resolved_path(self, tmp_path):
        out_file = tmp_path / "output.csv"
        result_path = write_output(SAMPLE_RESULTS, out_file)

        assert result_path.is_absolute()
        assert result_path == out_file.resolve()
