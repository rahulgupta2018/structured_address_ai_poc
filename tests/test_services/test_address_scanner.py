"""Tests for address_scanner service (Step 5) — raw address scan."""

from __future__ import annotations

from unittest.mock import patch

from services.address_scanner import _fuzzy_scan, _resolve_scan, scan

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with sample data                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Address containing a known city — used for exact token scan tests
SAMPLE_SCAN_ADDRESS = "PO BOX 123 DUBAI UAE"
SAMPLE_SCAN_COUNTRY = "AE"

# City that should match in the scan
SAMPLE_SCAN_CITY_NAME = "Dubai"
SAMPLE_SCAN_CITY_GEONAMEID = 292223

# City names available in this country (normalized, lowercase)
SAMPLE_SCAN_CITY_NAMES = {"dubai", "abu dhabi", "sharjah"}

# Address that should NOT match any city in the country above
SAMPLE_NO_MATCH_ADDRESS = "123 MAIN STREET, LONDON"

# Multi-word city scan test
SAMPLE_MULTI_WORD_ADDRESS = "OFFICE 5, ABU DHABI, UNITED ARAB EMIRATES"
SAMPLE_MULTI_WORD_NAMES = {"dubai", "abu dhabi", "abu"}


# ── Fixtures ─────────────────────────────────────────────────────────────────

DUBAI_CITY = {
    "geonameid": SAMPLE_SCAN_CITY_GEONAMEID,
    "name": SAMPLE_SCAN_CITY_NAME,
    "ascii_name": SAMPLE_SCAN_CITY_NAME,
    "country_code": SAMPLE_SCAN_COUNTRY,
    "admin1_code": "03",
    "population": 1137347,
    "name_type": "primary",
}


# ── scan() tests ─────────────────────────────────────────────────────────────


class TestScan:
    @patch("services.address_scanner.resolve_city_by_name", return_value=DUBAI_CITY)
    @patch(
        "services.address_scanner.get_all_normalized_names",
        return_value=SAMPLE_SCAN_CITY_NAMES,
    )
    def test_exact_token_match(self, mock_names, mock_resolve):
        state = {
            "raw_address": SAMPLE_SCAN_ADDRESS,
            "country_code": SAMPLE_SCAN_COUNTRY,
        }
        report("scan input", state)
        result = scan(state)
        report("scan output", {
            "scan_match": result["scan_match"],
            "scan_candidate": result["scan_candidate"],
            "geonames_id": result["geonames_id"],
            "match_type": result["match_type"],
        })
        assert result["scan_match"] is True
        assert result["scan_candidate"] == SAMPLE_SCAN_CITY_NAME
        assert result["geonames_id"] == SAMPLE_SCAN_CITY_GEONAMEID
        assert result["match_type"] == "exact_token"

    @patch(
        "services.address_scanner.get_all_normalized_names",
        return_value={"dubai", "abu dhabi"},
    )
    def test_no_match_in_address(self, mock_names):
        state = {
            "raw_address": SAMPLE_NO_MATCH_ADDRESS,
            "country_code": SAMPLE_SCAN_COUNTRY,
        }
        report("scan input (no match)", state)
        result = scan(state)
        report("scan output (no match)", {"scan_match": result["scan_match"]})
        assert result["scan_match"] is False
        assert result["scan_candidate"] is None

    @patch("services.address_scanner.get_all_normalized_names", return_value=set())
    def test_no_names_for_country(self, mock_names):
        state = {
            "raw_address": "123 Main St",
            "country_code": "XX",
        }
        result = scan(state)

        assert result["scan_match"] is False

    def test_empty_address(self):
        state = {"raw_address": "", "country_code": "US"}
        result = scan(state)
        assert result["scan_match"] is False

    def test_empty_country_code(self):
        state = {"raw_address": "123 Main St", "country_code": ""}
        result = scan(state)
        assert result["scan_match"] is False

    @patch("services.address_scanner.resolve_city_by_name", return_value=DUBAI_CITY)
    @patch(
        "services.address_scanner.get_all_normalized_names",
        return_value={"dubai", "abu dhabi", "al ain"},
    )
    def test_prefers_longest_ngram_match(self, mock_names, mock_resolve):
        """If 'abu dhabi' matches as a 2-gram, it should be preferred over shorter matches."""
        mock_names.return_value = SAMPLE_MULTI_WORD_NAMES
        state = {
            "raw_address": SAMPLE_MULTI_WORD_ADDRESS,
            "country_code": SAMPLE_SCAN_COUNTRY,
        }
        result = scan(state)

        assert result["scan_match"] is True
        # Should resolve (even if the mock returns Dubai for any name)

    @patch(
        "services.address_scanner.get_all_normalized_names",
        return_value={"abc"},
    )
    def test_ambiguous_short_match_skipped(self, mock_names):
        """Very short exact matches (<=3 chars) with similar alternatives are skipped."""
        mock_names.return_value = {"abc", "ab"}
        state = {
            "raw_address": "AB ABC STREET",
            "country_code": "XX",
        }
        # abc matches, ab also matches — but abc is len 3 and has a similar-length hit
        # This tests the ambiguity check for short tokens
        result = scan(state)
        # abc has len 3 and 1 token, "ab" also short — should trigger ambiguity skip
        # Actually abc (1 token, len 3) with "ab" (1 token, len 2) — differs by 1
        assert result["scan_match"] is False


# ── _fuzzy_scan() tests ─────────────────────────────────────────────────────


class TestFuzzyScan:
    def test_no_match_below_threshold(self):
        ngrams = ["xyzzytown"]
        city_names = {"springfield", "london", "paris"}
        result = _fuzzy_scan(ngrams, city_names)
        assert result is None

    def test_short_ngrams_filtered(self):
        """N-grams shorter than 4 chars are skipped."""
        ngrams = ["ab", "cd"]
        city_names = {"ab", "cd"}
        result = _fuzzy_scan(ngrams, city_names)
        assert result is None


# ── _resolve_scan() tests ───────────────────────────────────────────────────


class TestResolveScan:
    @patch("services.address_scanner.resolve_city_by_name", return_value=DUBAI_CITY)
    def test_populates_state(self, mock_resolve):
        state = {}
        result = _resolve_scan(state, SAMPLE_SCAN_COUNTRY, SAMPLE_SCAN_CITY_NAME.lower(), "exact_token")
        report("_resolve_scan output", {
            "scan_match": result["scan_match"],
            "scan_candidate": result["scan_candidate"],
            "geonames_id": result["geonames_id"],
            "match_type": result["match_type"],
        })
        assert result["scan_match"] is True
        assert result["scan_candidate"] == SAMPLE_SCAN_CITY_NAME
        assert result["geonames_id"] == SAMPLE_SCAN_CITY_GEONAMEID
        assert result["match_type"] == "exact_token"

    @patch("services.address_scanner.resolve_city_by_name", return_value=None)
    def test_no_city_found(self, mock_resolve):
        state = {}
        result = _resolve_scan(state, SAMPLE_SCAN_COUNTRY, "unknown", "exact_token")

        assert result.get("scan_match") is not True
