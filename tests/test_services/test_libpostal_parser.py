"""Tests for libpostal_parser service (Step 1) — real libpostal parsing, zero mocks.

Every test calls the real libpostal C parser via parse(). Sample addresses and
their expected results (discovered from actual libpostal output) are defined as
top-level constants. Edit the SAMPLE ADDRESSES block to test with your own data.

Requires: libpostal C library installed. The entire test module is skipped
automatically when the library is absent.
"""

from __future__ import annotations

from services.libpostal_parser import parse, _country_name_to_code

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with your own data               ║
# ║  Expected values were discovered by running each address through the    ║
# ║  real libpostal parser. If you change an address, re-run and update     ║
# ║  the corresponding expected values.                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# ── 1. Standard single-city address (Pakistan) ───────────────────────────
SINGLE_CITY_RAW = (
    "Plot 16-B, Punjab Small Industries Estate, "
    "Jhang Bahtra Road, Taxila, Pakistan"
)
SINGLE_CITY_EXPECTED_TOWN       = "taxila"
SINGLE_CITY_EXPECTED_CANDIDATES = ["taxila"]
SINGLE_CITY_EXPECTED_STREET     = "jhang bahtra road"
SINGLE_CITY_EXPECTED_BUILDING   = "plot 16-b punjab small industries estate"
SINGLE_CITY_EXPECTED_POSTAL     = None
SINGLE_CITY_EXPECTED_COUNTRY    = "pakistan"

# ── 2. Multiple city candidates — last "city" label wins (Thailand) ──────
MULTI_CITY_RAW = (
    "Villa E5, Malee Beach, 541/2 Moo 2, "
    "Long Beach Pra-Ae Beach, 81150 Krabi, Thailand"
)
MULTI_CITY_EXPECTED_TOWN       = "krabi"
MULTI_CITY_EXPECTED_CANDIDATES = ["long", "krabi"]
MULTI_CITY_EXPECTED_STREET     = "moo 2"
MULTI_CITY_EXPECTED_BUILDING   = "villa e5 malee beach"
MULTI_CITY_EXPECTED_POSTAL     = "81150"
MULTI_CITY_EXPECTED_COUNTRY    = "thailand"

# ── 3. Suburb-style address with two candidates (Australia) ───────────────
SUBURB_RAW = "123 Beach Rd, Bondi Beach, Sydney, Australia"
SUBURB_EXPECTED_TOWN       = "sydney"
SUBURB_EXPECTED_CANDIDATES = ["bondi beach", "sydney"]
SUBURB_EXPECTED_STREET     = "beach rd"
SUBURB_EXPECTED_BUILDING   = "123"
SUBURB_EXPECTED_POSTAL     = None
SUBURB_EXPECTED_COUNTRY    = "australia"

# ── 4. European address with postal code (Italy) ─────────────────────────
EUROPEAN_RAW = "Via Roma 15, 08042 Barisardo, Italy"
EUROPEAN_EXPECTED_TOWN       = "barisardo"
EUROPEAN_EXPECTED_CANDIDATES = ["barisardo"]
EUROPEAN_EXPECTED_STREET     = "via roma"
EUROPEAN_EXPECTED_BUILDING   = "15"
EUROPEAN_EXPECTED_POSTAL     = "08042"
EUROPEAN_EXPECTED_COUNTRY    = "italy"

# ── 5. Japanese address — city_district label (Japan) ─────────────────────
JAPANESE_RAW = "1-1 Marunouchi, Chiyoda-ku, Tokyo, Japan"
JAPANESE_EXPECTED_TOWN       = "chiyoda-ku"
JAPANESE_EXPECTED_CANDIDATES = ["marunouchi", "chiyoda-ku"]
JAPANESE_EXPECTED_STREET     = None
JAPANESE_EXPECTED_BUILDING   = "1-1"
JAPANESE_EXPECTED_POSTAL     = None
JAPANESE_EXPECTED_COUNTRY    = "japan"

# ── 6. Address with no city label at all ──────────────────────────────────
NO_CITY_RAW              = "12345 PO Box"
NO_CITY_EXPECTED_TOWN    = None
NO_CITY_EXPECTED_BUILDING = "12345"

# ── 7. Country-mismatch address (Germany in text, US as country_code) ────
MISMATCH_RAW = "123 Main St, Berlin, Germany"
MISMATCH_EXPECTED_TOWN       = "berlin"
MISMATCH_EXPECTED_STREET     = "main st"
MISMATCH_EXPECTED_BUILDING   = "123"
MISMATCH_EXPECTED_COUNTRY    = "germany"
MISMATCH_INPUT_CC            = "US"
MISMATCH_EXPECTED_CC         = "DE"
NO_MISMATCH_INPUT_CC         = "DE"

# ── 8. Empty / whitespace / missing address inputs ───────────────────────
EMPTY_ADDRESS      = ""
WHITESPACE_ADDRESS = "   "

# ── 9. Country-name lookup helper ────────────────────────────────────────
KNOWN_COUNTRY      = "germany"
KNOWN_COUNTRY_CODE = "DE"
UNKNOWN_COUNTRY    = "zzzzland"

# ── 10. Edge-case: no country in address text ────────────────────────────
NO_COUNTRY_IN_ADDRESS_INPUT_CC = "US"

# ── 11. Edge-case: preserving pre-existing state keys ────────────────────
PRESERVE_STATE_CC        = "PK"
PRESERVE_STATE_ROW_INDEX = 42
PRESERVE_STATE_JOB_ID    = "test-job"

# ── Expected warning strings (produced by parse()) ───────────────────────
WARN_EMPTY_ADDRESS        = "empty_address"
WARN_MULTIPLE_CANDIDATES  = "multiple_town_candidates"
WARN_NO_CITY_LABEL        = "libpostal_no_city_label"
WARN_COUNTRY_MISMATCH     = "country_code_mismatch_in_address"


# ═══════════════════════════════════════════════════════════════════════════
#  Test Classes — all call the REAL libpostal parser (zero mocks)
# ═══════════════════════════════════════════════════════════════════════════


class TestParseSingleCity:
    """parse() with a standard address that yields exactly one city candidate.

    Validates that town, street, building, country, postal code, and
    city_candidates are all extracted correctly by the real parser.
    """

    def test_pakistan_address(self):
        """Standard Pakistan address → single city candidate 'taxila'."""
        state = {"raw_address": SINGLE_CITY_RAW}
        result = parse(state)
        report("single city (Pakistan)", {
            "town": result["libpostal_town"],
            "candidates": result["libpostal_city_candidates"],
            "street": result["libpostal_street"],
            "building": result["libpostal_building"],
            "postal": result["libpostal_postal_code"],
            "country": result["libpostal_country"],
        })

        assert result["libpostal_town"] == SINGLE_CITY_EXPECTED_TOWN
        assert result["libpostal_city_candidates"] == SINGLE_CITY_EXPECTED_CANDIDATES
        assert result["libpostal_street"] == SINGLE_CITY_EXPECTED_STREET
        assert result["libpostal_building"] == SINGLE_CITY_EXPECTED_BUILDING
        assert result["libpostal_postal_code"] == SINGLE_CITY_EXPECTED_POSTAL
        assert result["libpostal_country"] == SINGLE_CITY_EXPECTED_COUNTRY

    def test_european_address_with_postal_code(self):
        """Italian address → city + postal code extracted correctly."""
        state = {"raw_address": EUROPEAN_RAW}
        result = parse(state)
        report("single city (Italy)", {
            "town": result["libpostal_town"],
            "candidates": result["libpostal_city_candidates"],
            "street": result["libpostal_street"],
            "building": result["libpostal_building"],
            "postal": result["libpostal_postal_code"],
            "country": result["libpostal_country"],
        })

        assert result["libpostal_town"] == EUROPEAN_EXPECTED_TOWN
        assert result["libpostal_city_candidates"] == EUROPEAN_EXPECTED_CANDIDATES
        assert result["libpostal_street"] == EUROPEAN_EXPECTED_STREET
        assert result["libpostal_building"] == EUROPEAN_EXPECTED_BUILDING
        assert result["libpostal_postal_code"] == EUROPEAN_EXPECTED_POSTAL
        assert result["libpostal_country"] == EUROPEAN_EXPECTED_COUNTRY


class TestParseMultipleCandidates:
    """parse() with addresses where libpostal returns multiple city-like tokens.

    The parse() wrapper selects the last explicit 'city' label as town.
    When only suburb/city_district labels are present, it falls back to
    the first candidate. A 'multiple_town_candidates' warning is added.
    """

    def test_thailand_address_last_city_wins(self):
        """Multiple candidates — 'krabi' (last city label) wins over 'long' (suburb)."""
        state = {"raw_address": MULTI_CITY_RAW}
        result = parse(state)
        report("multi city (Thailand)", {
            "town": result["libpostal_town"],
            "candidates": result["libpostal_city_candidates"],
            "warnings": result.get("warnings", []),
        })

        assert result["libpostal_town"] == MULTI_CITY_EXPECTED_TOWN
        assert result["libpostal_city_candidates"] == MULTI_CITY_EXPECTED_CANDIDATES
        assert result["libpostal_postal_code"] == MULTI_CITY_EXPECTED_POSTAL
        assert WARN_MULTIPLE_CANDIDATES in result["warnings"]

    def test_australia_suburb_and_city(self):
        """Bondi Beach (suburb) + Sydney (city) → Sydney wins as last 'city' label."""
        state = {"raw_address": SUBURB_RAW}
        result = parse(state)
        report("suburb + city (Australia)", {
            "town": result["libpostal_town"],
            "candidates": result["libpostal_city_candidates"],
            "street": result["libpostal_street"],
            "building": result["libpostal_building"],
            "warnings": result.get("warnings", []),
        })

        assert result["libpostal_town"] == SUBURB_EXPECTED_TOWN
        assert result["libpostal_city_candidates"] == SUBURB_EXPECTED_CANDIDATES
        assert result["libpostal_street"] == SUBURB_EXPECTED_STREET
        assert result["libpostal_building"] == SUBURB_EXPECTED_BUILDING
        assert WARN_MULTIPLE_CANDIDATES in result["warnings"]

    def test_japanese_address_city_district(self):
        """Japanese address — Chiyoda-ku (city_district) selected from multiple candidates."""
        state = {"raw_address": JAPANESE_RAW}
        result = parse(state)
        report("city_district (Japan)", {
            "town": result["libpostal_town"],
            "candidates": result["libpostal_city_candidates"],
            "building": result["libpostal_building"],
            "country": result["libpostal_country"],
            "warnings": result.get("warnings", []),
        })

        assert result["libpostal_town"] == JAPANESE_EXPECTED_TOWN
        assert result["libpostal_city_candidates"] == JAPANESE_EXPECTED_CANDIDATES
        assert result["libpostal_building"] == JAPANESE_EXPECTED_BUILDING
        assert result["libpostal_country"] == JAPANESE_EXPECTED_COUNTRY
        assert WARN_MULTIPLE_CANDIDATES in result["warnings"]


class TestParseNoCityLabel:
    """parse() when libpostal finds no city/suburb/city_district label.

    The address is too short or ambiguous for libpostal to identify a city.
    parse() sets town to None and adds a 'libpostal_no_city_label' warning.
    """

    def test_po_box_no_city(self):
        """[Negative] '12345 PO Box' → no city label found, town is None."""
        state = {"raw_address": NO_CITY_RAW}
        result = parse(state)
        report("no city label", {
            "town": result["libpostal_town"],
            "candidates": result["libpostal_city_candidates"],
            "building": result["libpostal_building"],
            "warnings": result.get("warnings", []),
        })

        assert result["libpostal_town"] == NO_CITY_EXPECTED_TOWN
        assert result["libpostal_city_candidates"] == []
        assert result["libpostal_building"] == NO_CITY_EXPECTED_BUILDING
        assert WARN_NO_CITY_LABEL in result["warnings"]


class TestParseEmptyAddress:
    """parse() with empty, whitespace-only, or missing raw_address.

    The empty-address guard fires before libpostal is called, returning
    all-None defaults and an 'empty_address' warning.
    """

    def test_empty_string(self):
        """[Negative] Empty raw_address → all fields None, 'empty_address' warning."""
        state = {"raw_address": EMPTY_ADDRESS}
        result = parse(state)
        report("empty address", {"result": result})

        assert result["libpostal_town"] is None
        assert result["libpostal_street"] is None
        assert result["libpostal_building"] is None
        assert result["libpostal_postal_code"] is None
        assert result["libpostal_country"] is None
        assert result["libpostal_city_candidates"] == []
        assert WARN_EMPTY_ADDRESS in result["warnings"]

    def test_whitespace_only(self):
        """[Negative] Whitespace-only raw_address → same as empty."""
        state = {"raw_address": WHITESPACE_ADDRESS}
        result = parse(state)
        report("whitespace address", {"result": result})

        assert result["libpostal_town"] is None
        assert WARN_EMPTY_ADDRESS in result["warnings"]

    def test_missing_raw_address_key(self):
        """[Negative] No raw_address key in state → same as empty."""
        state = {}
        result = parse(state)
        report("missing key", {"result": result})

        assert result["libpostal_town"] is None
        assert WARN_EMPTY_ADDRESS in result["warnings"]


class TestParseCountryMismatch:
    """Country-code mismatch detection when address text contains a country name.

    parse() resolves the country name to an ISO code and compares it
    against state['country_code']. A mismatch sets mismatch_detected=True
    and suggested_country_code.
    """

    def test_mismatch_germany_vs_us(self):
        """[Negative] Address says 'Germany' but country_code is 'US' → mismatch flagged."""
        state = {"raw_address": MISMATCH_RAW, "country_code": MISMATCH_INPUT_CC}
        result = parse(state)
        report("country mismatch (DE vs US)", {
            "town": result["libpostal_town"],
            "country": result["libpostal_country"],
            "mismatch": result.get("mismatch_detected"),
            "suggested": result.get("suggested_country_code"),
            "warnings": result.get("warnings", []),
        })

        assert result["libpostal_town"] == MISMATCH_EXPECTED_TOWN
        assert result["libpostal_street"] == MISMATCH_EXPECTED_STREET
        assert result["libpostal_country"] == MISMATCH_EXPECTED_COUNTRY
        assert result.get("mismatch_detected") is True
        assert result.get("suggested_country_code") == MISMATCH_EXPECTED_CC
        assert WARN_COUNTRY_MISMATCH in result["warnings"]

    def test_no_mismatch_when_matching(self):
        """Address says 'Germany' and country_code is 'DE' → no mismatch."""
        state = {"raw_address": MISMATCH_RAW, "country_code": NO_MISMATCH_INPUT_CC}
        result = parse(state)
        report("no mismatch (DE matches)", {
            "town": result["libpostal_town"],
            "mismatch": result.get("mismatch_detected", False),
            "warnings": result.get("warnings", []),
        })

        assert result["libpostal_town"] == MISMATCH_EXPECTED_TOWN
        assert "mismatch_detected" not in result
        assert WARN_COUNTRY_MISMATCH not in result["warnings"]

    def test_no_mismatch_when_no_country_in_address(self):
        """[Edge] Address has no country token → mismatch check is skipped."""
        state = {"raw_address": NO_CITY_RAW, "country_code": NO_COUNTRY_IN_ADDRESS_INPUT_CC}
        result = parse(state)
        report("no country in address", {
            "country": result["libpostal_country"],
            "mismatch": result.get("mismatch_detected", False),
        })

        assert result["libpostal_country"] is None
        assert "mismatch_detected" not in result

    def test_no_mismatch_when_no_country_code_in_state(self):
        """[Edge] No country_code in state → mismatch check skipped even with country detected."""
        state = {"raw_address": MISMATCH_RAW}
        result = parse(state)
        report("no country_code in state", {
            "country": result["libpostal_country"],
            "mismatch": result.get("mismatch_detected", False),
        })

        assert result["libpostal_country"] == MISMATCH_EXPECTED_COUNTRY
        assert "mismatch_detected" not in result


class TestParsePreservesState:
    """parse() preserves pre-existing keys in the state dict."""

    def test_extra_keys_survive(self):
        """[Edge] Pre-existing keys (country_code, row_index, etc.) survive parse()."""
        state = {
            "raw_address": SINGLE_CITY_RAW,
            "country_code": PRESERVE_STATE_CC,
            "row_index": PRESERVE_STATE_ROW_INDEX,
            "job_id": PRESERVE_STATE_JOB_ID,
        }
        result = parse(state)
        report("preserves state", {
            "country_code": result["country_code"],
            "row_index": result["row_index"],
            "job_id": result["job_id"],
            "town": result["libpostal_town"],
        })

        assert result["country_code"] == PRESERVE_STATE_CC
        assert result["row_index"] == PRESERVE_STATE_ROW_INDEX
        assert result["job_id"] == PRESERVE_STATE_JOB_ID
        assert result["libpostal_town"] == SINGLE_CITY_EXPECTED_TOWN


class TestCountryNameToCode:
    """_country_name_to_code() helper — ISO alpha-2 lookup from country name."""

    def test_known_country(self):
        """Common country name resolves to correct ISO alpha-2 code."""
        result = _country_name_to_code(KNOWN_COUNTRY)
        report("known country", {"input": KNOWN_COUNTRY, "output": result})
        assert result == KNOWN_COUNTRY_CODE

    def test_case_insensitive(self):
        """Lookup is case-insensitive (GERMANY → DE)."""
        result = _country_name_to_code(KNOWN_COUNTRY.upper())
        report("case insensitive", {"input": KNOWN_COUNTRY.upper(), "output": result})
        assert result == KNOWN_COUNTRY_CODE

    def test_unknown_country(self):
        """[Negative] Unknown country name returns None."""
        result = _country_name_to_code(UNKNOWN_COUNTRY)
        report("unknown country", {"input": UNKNOWN_COUNTRY, "output": result})
        assert result is None
