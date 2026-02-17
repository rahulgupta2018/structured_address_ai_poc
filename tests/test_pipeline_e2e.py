"""
End-to-end pipeline tests.

Uses a small in-memory test set to verify the full pipeline flow
without requiring actual libpostal or Ollama services.
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd

from src import config
from src.pipeline import run
from src.schemas import Status


@pytest.fixture
def sample_input_file(tmp_path: Path) -> Path:
    """Create a minimal test Excel file."""
    data = {
        "address_1": [
            "Marienplatz 1",
            "10 Downing Street",
            None,
            "123 Main St",
        ],
        "address_2": [
            "80331 München",
            "London SW1A 2AA",
            None,
            None,
        ],
        "address_3": [
            None,
            None,
            None,
            None,
        ],
        "country_code": [
            "DE",
            "GB",
            "US",
            "XX",  # Invalid, but the pipeline should handle it gracefully
        ],
    }
    filepath = tmp_path / "test_input.xlsx"
    pd.DataFrame(data).to_excel(filepath, index=False)
    return filepath


@pytest.fixture
def output_file(tmp_path: Path) -> Path:
    return tmp_path / "test_output.xlsx"


class TestPipelineSkipLLM:
    """Test the pipeline in deterministic-only mode (skip_llm=True)."""

    def test_runs_without_crash(self, sample_input_file: Path, output_file: Path):
        """The pipeline should complete without raising exceptions."""
        results = run(
            input_path=sample_input_file,
            output_path=output_file,
            skip_llm=True,
        )
        assert len(results) == 4
        assert output_file.exists()

    def test_empty_address_rejected(self, sample_input_file: Path, output_file: Path):
        """Row with all-null address fields should be rejected."""
        results = run(
            input_path=sample_input_file,
            output_path=output_file,
            skip_llm=True,
        )
        # Row index 2 has no address data
        empty_row = results[2]
        assert empty_row.status == Status.REJECTED
        assert empty_row.review_reason == "no_address_data"

    def test_output_has_all_columns(self, sample_input_file: Path, output_file: Path):
        """Output Excel should contain all expected columns."""
        run(
            input_path=sample_input_file,
            output_path=output_file,
            skip_llm=True,
        )
        df = pd.read_excel(output_file)
        expected = {
            "address_1", "address_2", "address_3", "country_code",
            "town", "street", "building", "postal_code",
            "status", "confidence_score", "parser_source",
            "geonames_match", "geonames_id", "normalized_town",
            "warnings", "review_reason",
        }
        assert expected.issubset(set(df.columns)), f"Missing: {expected - set(df.columns)}"

    def test_invariant_no_validated_without_geonames(
        self, sample_input_file: Path, output_file: Path
    ):
        """No row should be validated without geonames_match=True."""
        results = run(
            input_path=sample_input_file,
            output_path=output_file,
            skip_llm=True,
        )
        for r in results:
            if r.status == Status.VALIDATED:
                assert r.geonames_match is True, (
                    f"Invariant violation: row validated without GeoNames match. "
                    f"town={r.town}, source={r.parser_source}"
                )
