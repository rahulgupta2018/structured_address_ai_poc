"""Tests for postal_lookup service (Step 2) — disambiguation signal extraction."""

from __future__ import annotations

from unittest.mock import patch

from services.postal_lookup import lookup

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with your own data               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Postal code + country for the main lookup test
SAMPLE_POSTAL_CODE = "62701"
SAMPLE_POSTAL_COUNTRY = "US"

# Expected results from postal DB for the sample above
SAMPLE_POSTAL_PLACE = "Springfield"
SAMPLE_POSTAL_ADMIN1_CODE = "IL"
SAMPLE_POSTAL_REGION = "Illinois"
SAMPLE_POSTAL_LAT = 39.7817
SAMPLE_POSTAL_LON = -89.6501

# Postal code that returns no results
SAMPLE_POSTAL_NOTFOUND = "99999"


def _mock_postal_results():
    """Return a realistic query_postal_code response."""
    return [
        {
            "postal_code": SAMPLE_POSTAL_CODE,
            "place_name": SAMPLE_POSTAL_PLACE,
            "admin_name1": SAMPLE_POSTAL_REGION,
            "admin_code1": SAMPLE_POSTAL_ADMIN1_CODE,
            "country_code": SAMPLE_POSTAL_COUNTRY,
            "latitude": SAMPLE_POSTAL_LAT,
            "longitude": SAMPLE_POSTAL_LON,
        }
    ]


class TestPostalLookup:
    """postal_lookup.lookup() should extract admin1 disambiguation signals."""

    @patch("services.postal_lookup.query_postal_code", return_value=_mock_postal_results())
    def test_extracts_admin1_code(self, mock_qpc):
        state = {
            "libpostal_postal_code": SAMPLE_POSTAL_CODE,
            "country_code": SAMPLE_POSTAL_COUNTRY,
        }
        report("lookup input", state)
        result = lookup(state)
        report("lookup output", {
            "postal_town_candidate": result["postal_town_candidate"],
            "postal_admin1_code": result["postal_admin1_code"],
            "postal_region": result["postal_region"],
            "postal_city_hint": result["postal_city_hint"],
        })
        assert result["postal_town_candidate"] == SAMPLE_POSTAL_PLACE
        assert result["postal_admin1_code"] == SAMPLE_POSTAL_ADMIN1_CODE
        assert result["postal_region"] == SAMPLE_POSTAL_REGION
        assert result["postal_city_hint"] == SAMPLE_POSTAL_PLACE

    @patch("services.postal_lookup.query_postal_code", return_value=[])
    def test_no_results_leaves_none(self, mock_qpc):
        state = {
            "libpostal_postal_code": SAMPLE_POSTAL_NOTFOUND,
            "country_code": SAMPLE_POSTAL_COUNTRY,
        }
        report("lookup input (not found)", state)
        result = lookup(state)
        report("lookup output (not found)", {
            "postal_town_candidate": result["postal_town_candidate"],
            "postal_admin1_code": result["postal_admin1_code"],
        })
        assert result["postal_town_candidate"] is None
        assert result["postal_admin1_code"] is None
        assert result["postal_region"] is None
        assert result["postal_city_hint"] is None

    def test_no_postal_code_skips_lookup(self):
        state = {"libpostal_postal_code": None, "country_code": SAMPLE_POSTAL_COUNTRY}
        report("lookup input (no postal code)", state)
        result = lookup(state)
        report("lookup output (skipped)", {"postal_town_candidate": result["postal_town_candidate"]})
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
                "country_code": SAMPLE_POSTAL_COUNTRY,
                "latitude": 0.0,
                "longitude": 0.0,
            }
        ]
        state = {
            "libpostal_postal_code": "12345",
            "country_code": SAMPLE_POSTAL_COUNTRY,
        }
        report("lookup input (empty admin)", state)
        result = lookup(state)
        report("lookup output (empty admin)", {
            "postal_town_candidate": result["postal_town_candidate"],
            "postal_admin1_code": result["postal_admin1_code"],
            "postal_region": result["postal_region"],
        })
        assert result["postal_town_candidate"] == "SomePlace"
        assert result["postal_admin1_code"] is None  # empty → None
        assert result["postal_region"] is None        # empty → None
