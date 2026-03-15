"""Tests for io_reader service — file reading and column normalization."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import pytest

from services.io_reader import _clean, read_input

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with your own data               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Row 1: standard US address
SAMPLE_ADDR1_LINE1 = "123 Main St"
SAMPLE_ADDR1_LINE2 = "Apt 4"
SAMPLE_ADDR1_LINE3 = "Springfield"
SAMPLE_ADDR1_CC = "US"

# Row 2: GB address with empty address_2
SAMPLE_ADDR2_LINE1 = "456 Oak Ave"
SAMPLE_ADDR2_LINE3 = "London"
SAMPLE_ADDR2_CC = "GB"

# Row for alias tests
SAMPLE_ALIAS_LINE1 = "123 Main St"
SAMPLE_ALIAS_LINE2 = "Apt 4"
SAMPLE_ALIAS_LINE3 = "Town"
SAMPLE_ALIAS_CC = "US"

# Row for address_line alias tests
SAMPLE_LINE_ALIAS_1 = "Line 1"
SAMPLE_LINE_ALIAS_2 = "Line 2"
SAMPLE_LINE_ALIAS_3 = "Line 3"
SAMPLE_LINE_ALIAS_CC = "DE"

# Namibia edge case
SAMPLE_NAMIBIA_ADDR = "Windhoek Address"
SAMPLE_NAMIBIA_CC = "NA"


class TestClean:
    def test_normal_string(self):
        assert _clean("hello") == "hello"

    def test_strips_whitespace(self):
        assert _clean("  hello  ") == "hello"

    def test_none_returns_none(self):
        assert _clean(None) is None

    def test_empty_string_returns_none(self):
        assert _clean("") is None

    def test_whitespace_only_returns_none(self):
        assert _clean("   ") is None

    def test_nan_returns_none(self):
        assert _clean(float("nan")) is None


class TestReadInputCSV:
    def test_reads_csv(self, tmp_path):
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(
            "address_1,address_2,address_3,country_code\n"
            f"{SAMPLE_ADDR1_LINE1},{SAMPLE_ADDR1_LINE2},{SAMPLE_ADDR1_LINE3},{SAMPLE_ADDR1_CC}\n"
            f"{SAMPLE_ADDR2_LINE1},,{SAMPLE_ADDR2_LINE3},{SAMPLE_ADDR2_CC}\n"
        )
        rows = read_input(csv_file)
        report("read_input CSV", {"file": str(csv_file.name), "rows_read": len(rows), "row_0": rows[0], "row_1": rows[1]})
        assert len(rows) == 2
        assert rows[0]["address_1"] == SAMPLE_ADDR1_LINE1
        assert rows[0]["country_code"] == SAMPLE_ADDR1_CC
        assert rows[1]["address_2"] is None  # empty field → None
        assert rows[1]["country_code"] == SAMPLE_ADDR2_CC

    def test_column_aliases(self, tmp_path):
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(
            "addr_1,addr_2,addr_3,cc\n"
            f"{SAMPLE_ALIAS_LINE1},{SAMPLE_ALIAS_LINE2},{SAMPLE_ALIAS_LINE3},{SAMPLE_ALIAS_CC}\n"
        )
        rows = read_input(csv_file)

        assert len(rows) == 1
        assert rows[0]["address_1"] == SAMPLE_ALIAS_LINE1
        assert rows[0]["country_code"] == SAMPLE_ALIAS_CC

    def test_address_line_aliases(self, tmp_path):
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(
            "address_line_1,address_line_2,address_line_3,country\n"
            f"{SAMPLE_LINE_ALIAS_1},{SAMPLE_LINE_ALIAS_2},{SAMPLE_LINE_ALIAS_3},{SAMPLE_LINE_ALIAS_CC}\n"
        )
        rows = read_input(csv_file)

        assert len(rows) == 1
        assert rows[0]["address_1"] == SAMPLE_LINE_ALIAS_1
        assert rows[0]["country_code"] == SAMPLE_LINE_ALIAS_CC

    def test_skips_invalid_country_code(self, tmp_path):
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(
            "address_1,country_code\n"
            f"Valid Address,{SAMPLE_ADDR1_CC}\n"
            "Invalid Row,X\n"  # too short
        )
        rows = read_input(csv_file)

        assert len(rows) == 1
        assert rows[0]["country_code"] == SAMPLE_ADDR1_CC

    def test_country_code_uppercased(self, tmp_path):
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(
            "address_1,country_code\n"
            f"Test,{SAMPLE_ADDR2_CC.lower()}\n"
        )
        rows = read_input(csv_file)

        assert rows[0]["country_code"] == SAMPLE_ADDR2_CC

    def test_namibia_na_not_nan(self, tmp_path):
        """NA country code (Namibia) should not be converted to NaN."""
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(
            "address_1,country_code\n"
            f"{SAMPLE_NAMIBIA_ADDR},{SAMPLE_NAMIBIA_CC}\n"
        )
        rows = read_input(csv_file)
        report("read_input (Namibia NA)", {"country_code": rows[0]["country_code"] if rows else "EMPTY"})
        assert len(rows) == 1
        assert rows[0]["country_code"] == SAMPLE_NAMIBIA_CC


class TestReadInputExcel:
    def test_reads_xlsx(self, tmp_path):
        xlsx_file = tmp_path / "test.xlsx"
        df = pd.DataFrame({
            "address_1": [SAMPLE_ADDR1_LINE1],
            "address_2": [SAMPLE_ADDR1_LINE2],
            "address_3": [SAMPLE_ADDR1_LINE3],
            "country_code": [SAMPLE_ADDR1_CC],
        })
        df.to_excel(xlsx_file, index=False)

        rows = read_input(xlsx_file)

        assert len(rows) == 1
        assert rows[0]["address_1"] == SAMPLE_ADDR1_LINE1
        assert rows[0]["country_code"] == SAMPLE_ADDR1_CC


class TestReadInputErrors:
    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_input(tmp_path / "nonexistent.csv")

    def test_unsupported_format(self, tmp_path):
        txt_file = tmp_path / "test.txt"
        txt_file.write_text("some data")
        with pytest.raises(ValueError, match="Unsupported file format"):
            read_input(txt_file)

    def test_missing_country_code_column(self, tmp_path):
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(
            "address_1,address_2\n"
            "123 Main St,Apt 4\n"
        )
        with pytest.raises(ValueError, match="country_code"):
            read_input(csv_file)

    def test_column_names_normalized(self, tmp_path):
        """Column names with mixed case and spaces should be normalized."""
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(
            "Address 1,Address 2,Address 3,Country Code\n"
            f"{SAMPLE_ALIAS_LINE1},{SAMPLE_ALIAS_LINE2},{SAMPLE_ALIAS_LINE3},{SAMPLE_ALIAS_CC}\n"
        )
        rows = read_input(csv_file)

        assert len(rows) == 1
        assert rows[0]["address_1"] == SAMPLE_ALIAS_LINE1
