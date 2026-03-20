"""Tests for address_scanner service (Step 5) -- raw address scan.

Validates ``scan(state)``, ``_fuzzy_scan()``, and the full E2E pipeline
(Steps 1-5: parse -> lookup -> match -> detect -> scan).

The scanner tokenizes the raw address into n-grams and matches them against
all known city names for the given country code.  Phase 1 does exact n-gram
matching (prefers longest match, skips stopwords, rejects ambiguous short
tokens); Phase 2 does fuzzy matching via rapidfuzz.

All tests call the **real** service functions against the **real** SQLite
database -- zero mocks.
"""

from __future__ import annotations

import pytest

from services.address_scanner import _fuzzy_scan, scan
from services.geonames_repo import get_all_normalized_names
from services.libpostal_parser import parse
from services.postal_lookup import lookup
from services.geonames_exact import match
from services.mismatch_detector import detect
from utils.config import CONFIDENCE_FUZZY_SCAN

from tests.test_services.report import report


# ======================================================================
# SAMPLE DATA -- edit these to test with your own addresses
# ======================================================================

# -- Exact token match: city name is a clean token (no trailing punctuation) --
# (raw_address, country_code, expected_candidate, expected_geonames_id)
EXACT_TOKEN_SAMPLES = [
    ("PO BOX 123 DUBAI UAE",                                   "AE", "Dubai",         292223),
    ("123 Sukhumvit Road, Bangkok 10110, Thailand",             "TH", "Bangkok",       1609350),
    ("3-1-1 Marunouchi, Chiyoda-ku, Tokyo 100-0005",           "JP", "Tokyo",         1850147),
    ("Floor 12, Express Towers, Nariman Point, Mumbai 400021",  "IN", "Mumbai",        1275339),
    ("350 5th Avenue, New York, NY 10118",                      "US", "New York City", 5128581),
    ("10 Downing Street, London SW1A 2AA, UK",                  "GB", "London",        2643743),
]

# -- Fuzzy match: comma-attached tokens or noise prevent exact; fuzzy resolves --
# (raw_address, country_code, expected_candidate, expected_geonames_id)
FUZZY_MATCH_SAMPLES = [
    ("OFFICE 5, ABU DHABI, UNITED ARAB EMIRATES", "AE", "Abu Dhabi", 292968),
    ("123 Mall Road, Lahore, Pakistan",            "PK", "Lahore",    1172451),
    ("Block 2, Clifton, Karachi, Pakistan",         "PK", "Karachi",   1174872),
    ("25 Rue de la Place du Marche, Lyon",          "FR", "Lyon",      2996944),
]

# -- No match: city not in country's name set or unrecognisable text ----------
# (raw_address, country_code)
NO_MATCH_SAMPLES = [
    ("123 MAIN STREET NOWHERE SPECIAL",             "US"),  # gibberish location
    ("3-1-1 Marunouchi, Tokyo",                     "US"),  # Tokyo not in US names
    ("Via del Corso 126, 00186 Rome, Italy",         "IT"),  # tokenization noise
]

# -- Empty / missing input ---------------------------------------------------
# (raw_address, country_code)
EMPTY_INPUT_SAMPLES = [
    ("",             "US"),   # empty address
    ("   ",          "US"),   # whitespace-only address
    ("123 Main St",  ""),     # empty country code
]

# -- Stopword addresses: contain stopwords that must NOT match as city names --
# (raw_address, country_code, stopwords_present)
STOPWORD_SAMPLES = [
    # "de", "la", "du" are stopwords AND real city names in FR -- must be skipped
    ("25 Rue de la Place du Marche, Lyon", "FR", ["de", "la", "du"]),
    # "road" is a stopword AND a city name in GB -- must be skipped
    ("10 Downing Road, London SW1A 2AA",   "GB", ["road"]),
]

# -- Direct _fuzzy_scan() samples: (country, ngrams, expected_result) ---------
FUZZY_SCAN_MATCH_SAMPLES = [
    ("AU", ["melborne"],   "melbourne"),   # 1-char typo
    ("PK", ["karrachi"],   "karachi"),     # doubled consonant
    ("GB", ["manchster"],  "manchester"),  # missing vowel
]

FUZZY_SCAN_NO_MATCH_SAMPLES = [
    ("DE", ["berln"]),    # too short / divergent
    ("AU", ["mel"]),      # < 4 chars filtered
    ("PK", ["kar"]),      # < 4 chars filtered
]

# -- E2E pipeline: raw_address -> parse -> lookup -> match -> detect -> scan --
# (description, raw_address, country_code, expect_scan_match, expect_candidate,
#  expect_gid, expect_type)
E2E_STEP1_THROUGH_STEP5 = [
    # Steps 1-4 fail: unconventional format, scan catches the city
    ("scan-only Tokyo",
     "ATTN: SALES DEPT, TOKYO OFFICE, 100-0005 JAPAN", "JP",
     True, "Tokyo", 1850147, "exact_token"),
    # Steps 1-4 fail: freeform address, scan catches Mumbai
    ("scan-only Mumbai",
     "C/O RAJESH KUMAR, MUMBAI CENTRAL, 400008 INDIA", "IN",
     True, "Mumbai", 1275339, "exact_token"),
    # Steps 1-4 fail: Bangkok in industrial zone text
    ("scan-only Bangkok",
     "DISPATCH CENTER, BANGKOK INDUSTRIAL ZONE, 10120 THAILAND", "TH",
     True, "Bangkok", 1609350, "exact_token"),
    # Wrong country: London with AU code -- mismatch detected, scanner
    # tries GB first and finds "London" in the suggested country
    ("wrong-country London+AU",
     "10 Downing Street, London SW1A 2AA, United Kingdom", "AU",
     True, "London", 2643743, "exact_token"),
    # "Montana" is a US city name; scanner picks it up from freeform text
    ("Montana in freeform",
     "123 Rural Route 5, Middleofnowhere, Montana 59000", "US",
     True, "Montana City", 5666921, "exact_token"),
    # Full Thai address + wrong CC=US: mismatch detected, scanner tries TH
    # first and finds "Long" (a real TH city) via exact n-gram match
    ("Thai full addr+US",
     "Villa E5, Malee Beach, 541/2 Moo 2  Long Beach Pra-Ae Beach 81150 Krabi, Thailand", "US",
     True, "Long", 1152322, "exact_token"),
    # Full Pakistan address + CC=PK: Taxila not in DB, scan finds "Jhang Sadr"
    ("Pakistan full addr+PK",
     "Plot 16-B   Punjab Small Industries Estate  Jhang Bahtra Road, Taxila", "PK",
     True, "Jhang Sadr", 1175892, "exact_token"),
]


# ======================================================================
# TestScanExactToken -- Phase 1 exact n-gram match
# ======================================================================


class TestScanExactToken:
    """Addresses where the city name is a clean token and matches exactly."""

    @pytest.mark.parametrize(
        "raw_address, country_code, expected_candidate, expected_gid",
        EXACT_TOKEN_SAMPLES,
        ids=[s[2] for s in EXACT_TOKEN_SAMPLES],
    )
    def test_exact_token_match(
        self, raw_address, country_code, expected_candidate, expected_gid
    ):
        """scan() returns the expected city via exact n-gram matching."""
        state = {"raw_address": raw_address, "country_code": country_code}
        report("scan input", state)
        result = scan(state)
        report("scan output", {
            "scan_match": result["scan_match"],
            "scan_candidate": result["scan_candidate"],
            "geonames_id": result["geonames_id"],
            "match_type": result["match_type"],
            "match_confidence": result["match_confidence"],
        })
        assert result["scan_match"] is True
        assert result["scan_candidate"] == expected_candidate
        assert result["geonames_id"] == expected_gid
        assert result["match_type"] == "exact_token"
        assert result["match_confidence"] == CONFIDENCE_FUZZY_SCAN


# ======================================================================
# TestScanFuzzy -- Phase 2 fuzzy match via scan()
# ======================================================================


class TestScanFuzzy:
    """Addresses where exact match fails (e.g. comma-attached token) but fuzzy
    matching resolves the city."""

    @pytest.mark.parametrize(
        "raw_address, country_code, expected_candidate, expected_gid",
        FUZZY_MATCH_SAMPLES,
        ids=[s[2] for s in FUZZY_MATCH_SAMPLES],
    )
    def test_fuzzy_match(
        self, raw_address, country_code, expected_candidate, expected_gid
    ):
        """scan() resolves the city via Phase 2 fuzzy matching."""
        state = {"raw_address": raw_address, "country_code": country_code}
        report("scan input (fuzzy)", state)
        result = scan(state)
        report("scan output (fuzzy)", {
            "scan_match": result["scan_match"],
            "scan_candidate": result["scan_candidate"],
            "geonames_id": result["geonames_id"],
            "match_type": result["match_type"],
        })
        assert result["scan_match"] is True
        assert result["scan_candidate"] == expected_candidate
        assert result["geonames_id"] == expected_gid
        assert result["match_type"] == "fuzzy"
        assert result["match_confidence"] == CONFIDENCE_FUZZY_SCAN


# ======================================================================
# TestScanNoMatch -- scan returns False
# ======================================================================


class TestScanNoMatch:
    """Addresses where the scanner finds no matching city."""

    @pytest.mark.parametrize(
        "raw_address, country_code",
        NO_MATCH_SAMPLES,
        ids=["gibberish", "wrong_country", "tokenization_noise"],
    )
    def test_no_match(self, raw_address, country_code):
        """scan() sets scan_match=False when no city can be identified."""
        state = {"raw_address": raw_address, "country_code": country_code}
        report("scan input (no match)", state)
        result = scan(state)
        report("scan output (no match)", {"scan_match": result["scan_match"]})
        assert result["scan_match"] is False
        assert result["scan_candidate"] is None


# ======================================================================
# TestScanEmptyInput -- edge cases with empty / missing data
# ======================================================================


class TestScanEmptyInput:
    """Edge cases: empty / whitespace address or missing country code."""

    @pytest.mark.parametrize(
        "raw_address, country_code",
        EMPTY_INPUT_SAMPLES,
        ids=["empty_address", "whitespace_address", "empty_country"],
    )
    def test_empty_input(self, raw_address, country_code):
        """scan() returns early with scan_match=False for empty inputs."""
        state = {"raw_address": raw_address, "country_code": country_code}
        result = scan(state)
        assert result["scan_match"] is False
        assert result["scan_candidate"] is None


# ======================================================================
# TestScanStopwordSkipping -- stopwords must NOT match as cities
# ======================================================================


class TestScanStopwordSkipping:
    """Stopwords (prepositions, common address terms) that happen to be
    real city names must be skipped by the scanner."""

    @pytest.mark.parametrize(
        "raw_address, country_code, stopwords_present",
        STOPWORD_SAMPLES,
        ids=["french_prepositions", "english_road"],
    )
    def test_stopwords_not_matched_as_city(
        self, raw_address, country_code, stopwords_present
    ):
        """Stopwords in the address are NOT returned as scan_candidate."""
        state = {"raw_address": raw_address, "country_code": country_code}
        result = scan(state)
        report("scan stopword test", {
            "stopwords_present": stopwords_present,
            "scan_candidate": result.get("scan_candidate"),
        })
        candidate = result.get("scan_candidate")
        # If a match is found, it must NOT be one of the stopword tokens
        if candidate:
            candidate_lower = candidate.lower()
            for sw in stopwords_present:
                assert candidate_lower != sw, (
                    f"Stopword '{sw}' should not be returned as scan_candidate"
                )


# ======================================================================
# TestFuzzyScanDirect -- direct _fuzzy_scan() function tests
# ======================================================================


class TestFuzzyScanDirect:
    """Tests the internal _fuzzy_scan() function with real city name sets."""

    @pytest.mark.parametrize(
        "country_code, ngrams, expected_name",
        FUZZY_SCAN_MATCH_SAMPLES,
        ids=[s[1][0] for s in FUZZY_SCAN_MATCH_SAMPLES],
    )
    def test_fuzzy_match(self, country_code, ngrams, expected_name):
        """Misspelled city names are resolved by fuzzy matching."""
        city_names = get_all_normalized_names(country_code)
        result = _fuzzy_scan(ngrams, city_names)
        report("_fuzzy_scan", {
            "input_ngrams": ngrams,
            "country": country_code,
            "result": result,
        })
        assert result == expected_name

    @pytest.mark.parametrize(
        "country_code, ngrams",
        FUZZY_SCAN_NO_MATCH_SAMPLES,
        ids=["too_divergent", "short_au", "short_pk"],
    )
    def test_fuzzy_no_match(self, country_code, ngrams):
        """Fuzzy matching returns None for too-short or too-divergent ngrams."""
        city_names = get_all_normalized_names(country_code)
        result = _fuzzy_scan(ngrams, city_names)
        assert result is None


# ======================================================================
# TestScanAmbiguousShort -- short-token ambiguity check
# ======================================================================


class TestScanAmbiguousShort:
    """Very short exact matches (<=3 chars, 1 token) with similar-length
    alternatives trigger the ambiguity guard and are skipped."""

    def test_ambiguous_short_tokens_rejected(self):
        """Short ambiguous tokens cause scan to skip exact match."""
        state = {"raw_address": "AB AC STREET", "country_code": "US"}
        result = scan(state)
        report("ambiguous short", {"scan_match": result["scan_match"]})
        assert result["scan_match"] is False


# ======================================================================
# TestScanPreservesState -- scan must NOT clobber existing state fields
# ======================================================================


class TestScanPreservesState:
    """Running scan() must not overwrite state fields set by earlier steps."""

    def test_preserves_existing_fields(self):
        """Pre-existing state keys (town, postal_code, exact_match) survive."""
        state = {
            "raw_address": "PO BOX 123 DUBAI UAE",
            "country_code": "AE",
            "town": "preexisting_town",
            "postal_code": "12345",
            "exact_match": True,
            "town_candidate": "SomeCity",
            "mismatch": False,
        }
        result = scan(state)
        assert result["town"] == "preexisting_town"
        assert result["postal_code"] == "12345"
        assert result["exact_match"] is True
        assert result["town_candidate"] == "SomeCity"
        assert result["mismatch"] is False
        # scan still populates its own fields
        assert result["scan_match"] is True


# ======================================================================
# TestScanFromRawAddress -- E2E: Steps 1-5 pipeline
# ======================================================================


class TestScanFromRawAddress:
    """End-to-end: raw address -> parse -> lookup -> match -> detect -> scan.

    Simulates the real pipeline flow (Steps 1 through 5).  These tests verify
    that the scanner can find cities in addresses that earlier steps may or
    may not have resolved.
    """

    @pytest.mark.parametrize(
        "desc, raw_address, country_code, expect_scan_match, "
        "expect_candidate, expect_gid, expect_type",
        E2E_STEP1_THROUGH_STEP5,
        ids=[s[0] for s in E2E_STEP1_THROUGH_STEP5],
    )
    def test_e2e_pipeline(
        self,
        desc,
        raw_address,
        country_code,
        expect_scan_match,
        expect_candidate,
        expect_gid,
        expect_type,
    ):
        """Full pipeline: parse -> lookup -> match -> detect -> scan."""
        state = {"raw_address": raw_address, "country_code": country_code}
        report(f"E2E input [{desc}]", state)

        parse(state)
        lookup(state)
        match(state)
        detect(state)
        scan(state)

        report(f"E2E output [{desc}]", {
            "town_parsed": state.get("town"),
            "exact_match": state.get("exact_match"),
            "town_candidate": state.get("town_candidate"),
            "scan_match": state.get("scan_match"),
            "scan_candidate": state.get("scan_candidate"),
            "geonames_id": state.get("geonames_id"),
            "match_type": state.get("match_type"),
        })

        assert state["scan_match"] is expect_scan_match
        if expect_candidate is not None:
            assert state["scan_candidate"] == expect_candidate
        if expect_gid is not None:
            assert state["geonames_id"] == expect_gid
        if expect_type is not None:
            assert state["match_type"] == expect_type
