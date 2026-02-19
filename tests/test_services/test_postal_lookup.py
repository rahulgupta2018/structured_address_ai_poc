"""Tests for postal_lookup service (Step 2) — disambiguation signal extraction."""

from __future__ import annotations

from unittest.mock import patch

from services.postal_lookup import lookup


def _mock_postal_results():
    """Return a realistic query_postal_code response for US/62701."""
    return [
        {
            "postal_code": "62701",
            "place_name": "Springfield",
            "admin_name1": "Illinois",
            "admin_code1": "IL",
            "country_code": "US",
            "latitude": 39.7817,
            "longitude": -89.6501,
        }
    ]


class TestPostalLookup:
    """postal_lookup.lookup() should extract admin1 disambiguation signals."""

    @patch("services.postal_lookup.query_postal_code", return_value=_mock_postal_results())
    def test_extracts_admin1_code(self, mock_qpc):
        state = {
            "libpostal_postal_code": "62701",
            "country_code": "US",
        }
        result = lookup(state)

        assert result["postal_town_candidate"] == "Springfield"
        assert result["postal_admin1_code"] == "IL"
        assert result["postal_region"] == "Illinois"
        assert result["postal_city_hint"] == "Springfield"

    @patch("services.postal_lookup.query_postal_code", return_value=[])
    def test_no_results_leaves_none(self, mock_qpc):
        state = {
            "libpostal_postal_code": "99999",
            "country_code": "US",
        }
        result = lookup(state)

        assert result["postal_town_candidate"] is None
        assert result["postal_admin1_code"] is None
        assert result["postal_region"] is None
        assert result["postal_city_hint"] is None

    def test_no_postal_code_skips_lookup(self):
        state = {"libpostal_postal_code": None, "country_code": "US"}
        result = lookup(state)

        assert result["postal_town_candidate"] is None
        assert result["postal_admin1_code"] is None

    @patch("services.postal_lookup.query_postal_code")
    def test_missing_admin_fields_set_none(self, mock_qpc):
        """If postal DB row has empty admin fields, store None not empty string."""
        mock_qpc.return_value = [
            {
                "postal_code": "12345",
                "place_name": "SomePlace",
                "admin_name1": "",
                "admin_code1": "",
                "country_code": "US",
                "latitude": 0.0,
                "longitude": 0.0,
            }
        ]
        state = {
            "libpostal_postal_code": "12345",
            "country_code": "US",
        }
        result = lookup(state)

        assert result["postal_town_candidate"] == "SomePlace"
        assert result["postal_admin1_code"] is None  # empty → None
        assert result["postal_region"] is None        # empty → None
