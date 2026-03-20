"""Tests for postal_lookup service (Step 2) — real database lookups, zero mocks.

Every test calls the real lookup() function which queries the GeoNames SQLite
postal_codes table. Sample postal codes and their expected results (discovered
from actual database output) are defined as top-level constants.

Requires: GeoNames SQLite database at the configured path.
"""

from __future__ import annotations

import pytest

from services.libpostal_parser import parse
from services.postal_lookup import lookup

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE DATA & EXPECTED RESULTS — edit here, not inside tests            ║
# ║  Expected values were discovered by running each postal code through     ║
# ║  the real GeoNames database. If you change the DB, re-run discovery      ║
# ║  and update these constants.                                             ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# ── Happy-path samples: (postal, country, expected_town, expected_admin1,
#    expected_region, description) ──────────────────────────────────────────
HAPPY_PATH_SAMPLES = [
    ("62701",    "US", "Springfield", "IL", "Illinois",        "US Springfield"),
    ("08042",    "IT", "Bari Sardo",  "14", "Sardegna",        "Italy Bari Sardo"),
    ("81150",    "TH", "Ko Lanta",    "81", "Krabi",           "Thailand Krabi"),
    ("10115",    "DE", "Berlin",      "BE", "Berlin",          "Germany Berlin"),
    ("100-0001", "JP", "Chiyoda",     "40", "Tokyo To",        "Japan Chiyoda"),
    ("2026",     "AU", "Ben Buckler", "NSW","New South Wales",  "Australia multi-row first-alphabetical"),
]

# ── Negative samples: postal codes that should yield all-None outputs ─────
#    (postal, country, description)
NO_RESULT_SAMPLES = [
    ("00000", "US", "non-existent postal code"),
]

# ── Suggested-country fallback: postal + wrong CC + suggested_cc from Step 1 ─
#    (postal, country, suggested_cc, exp_town, exp_admin1, exp_region, method, desc)
SUGGESTED_COUNTRY_SAMPLES = [
    ("62701", "DE", "US", "Springfield", "IL", "Illinois",
     "suggested_country", "US postal with wrong CC=DE, suggested=US"),
    ("81150", "US", "TH", "Ko Lanta",    "81", "Krabi",
     "suggested_country", "TH postal with wrong CC=US, suggested=TH"),
    ("10115", "XX", "DE", "Berlin",      "BE", "Berlin",
     "suggested_country", "DE postal with invalid CC=XX, suggested=DE"),
]

# ── Postal-only fallback: no country_code, postal is globally unambiguous ──
#    (postal, exp_town, exp_admin1, exp_region, desc)
POSTAL_ONLY_UNAMBIGUOUS_SAMPLES = [
    ("62701", "Springfield", "IL", "Illinois",
     "62701 exists only in US — resolves without country"),
]

# ── Postal-only ambiguous: multi-country postal, no suggested_cc → None ────
#    (postal, country, desc)
POSTAL_ONLY_AMBIGUOUS_SAMPLES = [
    ("81150", "US", "81150 in FR/PK/TH/UA — ambiguous without suggested_cc"),
    ("10115", "XX", "10115 in 7 countries — ambiguous without suggested_cc"),
]

# ── Missing-input samples: states where lookup returns early (no postal) ──
#    (state_dict, description)
MISSING_INPUT_SAMPLES = [
    ({"libpostal_postal_code": None,    "country_code": "US"},  "postal_code is None"),
    ({},                                                         "completely empty state"),
]

# ── Whitespace-padded postal code ─────────────────────────────────────────
WHITESPACE_POSTAL    = "  62701  "
WHITESPACE_COUNTRY   = "US"
WHITESPACE_EXPECTED_TOWN   = "Springfield"
WHITESPACE_EXPECTED_ADMIN1 = "IL"
WHITESPACE_EXPECTED_REGION = "Illinois"

# ── Pre-existing state keys to check preservation ────────────────────────
PRESERVE_POSTAL      = "62701"
PRESERVE_COUNTRY     = "US"
PRESERVE_EXTRA_KEY   = "preserved_value"
PRESERVE_ROW_INDEX   = 99
PRESERVE_JOB_ID      = "test-batch-42"
PRESERVE_EXPECTED_TOWN = "Springfield"

# ── Raw-address end-to-end samples: raw address → parse() → lookup() ──────
#    Simulates the real pipeline flow (Step 1 → Step 2).
#    (raw_address, country_code, expected_town, expected_admin1, expected_region, desc)
RAW_ADDRESS_SAMPLES = [
    (
        "Plot 16-B, Punjab Small Industries Estate, "
        "Jhang Bahtra Road, Taxila, Pakistan",
        "PK", None, None, None,
        "Pakistan address without postal code",
    ),
    (
        "Via Roma 15, 08042 Barisardo, Italy",
        "IT", "Bari Sardo", "14", "Sardegna",
        "Italy address with postal code 08042",
    ),
    (
        "Villa E5, Malee Beach, 541/2 Moo 2, "
        "Long Beach Pra-Ae Beach, 81150 Krabi, Thailand",
        "TH", "Ko Lanta", "81", "Krabi",
        "Thailand address with postal code 81150",
    ),
    (
        "1-1 Marunouchi, Chiyoda-ku, Tokyo, Japan",
        "JP", None, None, None,
        "Japan address without postal code",
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
#  Helper — build a state dict for lookup()
# ═══════════════════════════════════════════════════════════════════════════

def _state(postal_code, country_code):
    """Build a minimal state dict for lookup()."""
    return {
        "libpostal_postal_code": postal_code,
        "country_code": country_code,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Test Classes — all call the REAL lookup() against the REAL SQLite DB
# ═══════════════════════════════════════════════════════════════════════════


class TestLookupHappyPath:
    """lookup() with valid postal codes from multiple countries.

    Each sample is a (postal, country, expected_town, expected_admin1,
    expected_region) tuple. The same generic assertions run for every sample.
    """

    @pytest.mark.parametrize(
        "postal, country, exp_town, exp_admin1, exp_region, desc",
        HAPPY_PATH_SAMPLES,
        ids=[s[5] for s in HAPPY_PATH_SAMPLES],
    )
    def test_town_candidate(self, postal, country, exp_town, exp_admin1, exp_region, desc):
        """Postal code resolves to the expected town/place name."""
        result = lookup(_state(postal, country))
        report(f"lookup [{desc}]", {
            "postal_town_candidate": result["postal_town_candidate"],
            "postal_admin1_code": result["postal_admin1_code"],
            "postal_region": result["postal_region"],
        })
        assert result["postal_town_candidate"] == exp_town

    @pytest.mark.parametrize(
        "postal, country, exp_town, exp_admin1, exp_region, desc",
        HAPPY_PATH_SAMPLES,
        ids=[s[5] for s in HAPPY_PATH_SAMPLES],
    )
    def test_admin1_code(self, postal, country, exp_town, exp_admin1, exp_region, desc):
        """Postal code extracts the correct admin1 code."""
        result = lookup(_state(postal, country))
        assert result["postal_admin1_code"] == exp_admin1

    @pytest.mark.parametrize(
        "postal, country, exp_town, exp_admin1, exp_region, desc",
        HAPPY_PATH_SAMPLES,
        ids=[s[5] for s in HAPPY_PATH_SAMPLES],
    )
    def test_region_name(self, postal, country, exp_town, exp_admin1, exp_region, desc):
        """Postal code extracts the correct admin1 region name."""
        result = lookup(_state(postal, country))
        assert result["postal_region"] == exp_region

    @pytest.mark.parametrize(
        "postal, country, exp_town, exp_admin1, exp_region, desc",
        HAPPY_PATH_SAMPLES,
        ids=[s[5] for s in HAPPY_PATH_SAMPLES],
    )
    def test_city_hint_equals_town(self, postal, country, exp_town, exp_admin1, exp_region, desc):
        """postal_city_hint is an alias — always equals postal_town_candidate."""
        result = lookup(_state(postal, country))
        assert result["postal_city_hint"] == exp_town
        assert result["postal_city_hint"] == result["postal_town_candidate"]

    @pytest.mark.parametrize(
        "postal, country, exp_town, exp_admin1, exp_region, desc",
        HAPPY_PATH_SAMPLES,
        ids=[s[5] for s in HAPPY_PATH_SAMPLES],
    )
    def test_lookup_method_is_primary(self, postal, country, exp_town, exp_admin1, exp_region, desc):
        """Direct postal+country match should report method='primary'."""
        result = lookup(_state(postal, country))
        assert result["postal_lookup_method"] == "primary"


class TestLookupNoResults:
    """lookup() when the postal code is not found in the database.

    Covers non-existent codes and valid codes queried against the wrong country.
    All four output fields should be None.
    """

    @pytest.mark.parametrize(
        "postal, country, desc",
        NO_RESULT_SAMPLES,
        ids=[s[2] for s in NO_RESULT_SAMPLES],
    )
    def test_all_fields_none(self, postal, country, desc):
        """[Negative] No DB match → all output fields are None."""
        result = lookup(_state(postal, country))
        report(f"no-result [{desc}]", {
            "postal_town_candidate": result["postal_town_candidate"],
            "postal_admin1_code": result["postal_admin1_code"],
            "postal_region": result["postal_region"],
            "postal_city_hint": result["postal_city_hint"],
        })
        assert result["postal_town_candidate"] is None
        assert result["postal_admin1_code"] is None
        assert result["postal_region"] is None
        assert result["postal_city_hint"] is None
        assert result["postal_lookup_method"] is None


class TestLookupMissingInputs:
    """lookup() when the postal code itself is missing.

    The function returns early (before any DB query) when there is no postal
    code, setting all output fields to None via setdefault.
    """

    @pytest.mark.parametrize(
        "state, desc",
        MISSING_INPUT_SAMPLES,
        ids=[s[1] for s in MISSING_INPUT_SAMPLES],
    )
    def test_skip_lookup_all_none(self, state, desc):
        """[Negative] Missing/empty input → skip lookup, all fields None."""
        result = lookup(dict(state))
        report(f"missing input [{desc}]", {
            "postal_town_candidate": result["postal_town_candidate"],
            "postal_admin1_code": result["postal_admin1_code"],
        })
        assert result["postal_town_candidate"] is None
        assert result["postal_admin1_code"] is None
        assert result["postal_region"] is None
        assert result["postal_city_hint"] is None
        assert result["postal_lookup_method"] is None


class TestLookupWhitespace:
    """lookup() when postal code has leading/trailing whitespace.

    The underlying query_postal_code() strips whitespace before querying,
    so padded input still matches the same DB rows.
    """

    def test_padded_postal_still_matches(self):
        """[Edge] '  62701  ' with spaces resolves same as '62701'."""
        result = lookup(_state(WHITESPACE_POSTAL, WHITESPACE_COUNTRY))
        report("whitespace postal", {
            "postal_town_candidate": result["postal_town_candidate"],
            "postal_admin1_code": result["postal_admin1_code"],
        })
        assert result["postal_town_candidate"] == WHITESPACE_EXPECTED_TOWN
        assert result["postal_admin1_code"] == WHITESPACE_EXPECTED_ADMIN1
        assert result["postal_region"] == WHITESPACE_EXPECTED_REGION


class TestLookupPreservesState:
    """lookup() preserves pre-existing keys in the state dict."""

    def test_extra_keys_survive(self):
        """[Edge] Pre-existing keys (extra_key, row_index, job_id) survive lookup()."""
        state = _state(PRESERVE_POSTAL, PRESERVE_COUNTRY)
        state["extra_key"] = PRESERVE_EXTRA_KEY
        state["row_index"] = PRESERVE_ROW_INDEX
        state["job_id"] = PRESERVE_JOB_ID
        result = lookup(state)
        report("preserves state", {
            "extra_key": result["extra_key"],
            "row_index": result["row_index"],
            "job_id": result["job_id"],
            "postal_town_candidate": result["postal_town_candidate"],
        })
        assert result["extra_key"] == PRESERVE_EXTRA_KEY
        assert result["row_index"] == PRESERVE_ROW_INDEX
        assert result["job_id"] == PRESERVE_JOB_ID
        assert result["postal_town_candidate"] == PRESERVE_EXPECTED_TOWN


class TestLookupFromRawAddress:
    """End-to-end: raw address → parse() (Step 1) → lookup() (Step 2).

    Simulates the real pipeline flow. The raw address is first parsed by
    libpostal to extract the postal code, then the result is fed into
    lookup() to resolve the postal town, admin1, and region from the DB.
    Addresses without a postal code should yield all-None lookup fields.
    """

    @pytest.mark.parametrize(
        "raw, country, exp_town, exp_admin1, exp_region, desc",
        RAW_ADDRESS_SAMPLES,
        ids=[s[5] for s in RAW_ADDRESS_SAMPLES],
    )
    def test_town_candidate(self, raw, country, exp_town, exp_admin1, exp_region, desc):
        """Raw address → parse → lookup resolves expected town candidate."""
        state = parse({"raw_address": raw, "country_code": country})
        result = lookup(state)
        report(f"raw-address [{desc}]", {
            "libpostal_postal_code": state.get("libpostal_postal_code"),
            "postal_town_candidate": result["postal_town_candidate"],
            "postal_admin1_code": result["postal_admin1_code"],
            "postal_region": result["postal_region"],
        })
        assert result["postal_town_candidate"] == exp_town

    @pytest.mark.parametrize(
        "raw, country, exp_town, exp_admin1, exp_region, desc",
        RAW_ADDRESS_SAMPLES,
        ids=[s[5] for s in RAW_ADDRESS_SAMPLES],
    )
    def test_admin1_code(self, raw, country, exp_town, exp_admin1, exp_region, desc):
        """Raw address → parse → lookup extracts expected admin1 code."""
        state = parse({"raw_address": raw, "country_code": country})
        result = lookup(state)
        assert result["postal_admin1_code"] == exp_admin1

    @pytest.mark.parametrize(
        "raw, country, exp_town, exp_admin1, exp_region, desc",
        RAW_ADDRESS_SAMPLES,
        ids=[s[5] for s in RAW_ADDRESS_SAMPLES],
    )
    def test_region_name(self, raw, country, exp_town, exp_admin1, exp_region, desc):
        """Raw address → parse → lookup extracts expected region name."""
        state = parse({"raw_address": raw, "country_code": country})
        result = lookup(state)
        assert result["postal_region"] == exp_region


class TestLookupSuggestedCountryFallback:
    """Fallback: postal_code + suggested_country_code from Step 1 mismatch.

    When the primary lookup (postal + country) returns nothing but Step 1
    detected a country mismatch and provided suggested_country_code, the
    service retries with the suggested country. This is critical for
    addresses where the user-provided country code is wrong.
    """

    @pytest.mark.parametrize(
        "postal, country, suggested_cc, exp_town, exp_admin1, exp_region, exp_method, desc",
        SUGGESTED_COUNTRY_SAMPLES,
        ids=[s[7] for s in SUGGESTED_COUNTRY_SAMPLES],
    )
    def test_town_candidate(self, postal, country, suggested_cc, exp_town, exp_admin1, exp_region, exp_method, desc):
        """Suggested-country fallback resolves the expected town."""
        state = _state(postal, country)
        state["suggested_country_code"] = suggested_cc
        result = lookup(state)
        report(f"suggested-cc [{desc}]", {
            "postal_town_candidate": result["postal_town_candidate"],
            "postal_lookup_method": result["postal_lookup_method"],
        })
        assert result["postal_town_candidate"] == exp_town

    @pytest.mark.parametrize(
        "postal, country, suggested_cc, exp_town, exp_admin1, exp_region, exp_method, desc",
        SUGGESTED_COUNTRY_SAMPLES,
        ids=[s[7] for s in SUGGESTED_COUNTRY_SAMPLES],
    )
    def test_admin1_code(self, postal, country, suggested_cc, exp_town, exp_admin1, exp_region, exp_method, desc):
        """Suggested-country fallback extracts the correct admin1 code."""
        state = _state(postal, country)
        state["suggested_country_code"] = suggested_cc
        result = lookup(state)
        assert result["postal_admin1_code"] == exp_admin1

    @pytest.mark.parametrize(
        "postal, country, suggested_cc, exp_town, exp_admin1, exp_region, exp_method, desc",
        SUGGESTED_COUNTRY_SAMPLES,
        ids=[s[7] for s in SUGGESTED_COUNTRY_SAMPLES],
    )
    def test_region_name(self, postal, country, suggested_cc, exp_town, exp_admin1, exp_region, exp_method, desc):
        """Suggested-country fallback extracts the correct region name."""
        state = _state(postal, country)
        state["suggested_country_code"] = suggested_cc
        result = lookup(state)
        assert result["postal_region"] == exp_region

    @pytest.mark.parametrize(
        "postal, country, suggested_cc, exp_town, exp_admin1, exp_region, exp_method, desc",
        SUGGESTED_COUNTRY_SAMPLES,
        ids=[s[7] for s in SUGGESTED_COUNTRY_SAMPLES],
    )
    def test_lookup_method(self, postal, country, suggested_cc, exp_town, exp_admin1, exp_region, exp_method, desc):
        """Suggested-country fallback reports correct lookup method."""
        state = _state(postal, country)
        state["suggested_country_code"] = suggested_cc
        result = lookup(state)
        assert result["postal_lookup_method"] == exp_method


class TestLookupPostalOnlyFallback:
    """Fallback: postal code alone resolves when it is globally unambiguous.

    When both primary and suggested-country lookups fail (or suggested_cc
    is absent), the service queries the postal code without a country filter.
    If all rows point to a single country, the result is used.
    """

    @pytest.mark.parametrize(
        "postal, exp_town, exp_admin1, exp_region, desc",
        POSTAL_ONLY_UNAMBIGUOUS_SAMPLES,
        ids=[s[4] for s in POSTAL_ONLY_UNAMBIGUOUS_SAMPLES],
    )
    def test_town_candidate(self, postal, exp_town, exp_admin1, exp_region, desc):
        """Globally-unique postal code resolves to expected town."""
        state = {"libpostal_postal_code": postal, "country_code": ""}
        result = lookup(state)
        report(f"postal-only [{desc}]", {
            "postal_town_candidate": result["postal_town_candidate"],
            "postal_lookup_method": result["postal_lookup_method"],
        })
        assert result["postal_town_candidate"] == exp_town

    @pytest.mark.parametrize(
        "postal, exp_town, exp_admin1, exp_region, desc",
        POSTAL_ONLY_UNAMBIGUOUS_SAMPLES,
        ids=[s[4] for s in POSTAL_ONLY_UNAMBIGUOUS_SAMPLES],
    )
    def test_admin1_code(self, postal, exp_town, exp_admin1, exp_region, desc):
        """Globally-unique postal code extracts correct admin1 code."""
        state = {"libpostal_postal_code": postal, "country_code": ""}
        result = lookup(state)
        assert result["postal_admin1_code"] == exp_admin1

    @pytest.mark.parametrize(
        "postal, exp_town, exp_admin1, exp_region, desc",
        POSTAL_ONLY_UNAMBIGUOUS_SAMPLES,
        ids=[s[4] for s in POSTAL_ONLY_UNAMBIGUOUS_SAMPLES],
    )
    def test_lookup_method(self, postal, exp_town, exp_admin1, exp_region, desc):
        """Globally-unique postal code reports method='postal_only'."""
        state = {"libpostal_postal_code": postal, "country_code": ""}
        result = lookup(state)
        assert result["postal_lookup_method"] == "postal_only"

    def test_wrong_country_unambiguous_postal(self):
        """[Edge] 62701+DE: wrong CC, no suggested — resolves via postal-only (US-only code)."""
        result = lookup(_state("62701", "DE"))
        assert result["postal_town_candidate"] == "Springfield"
        assert result["postal_lookup_method"] == "postal_only"


class TestLookupPostalOnlyAmbiguous:
    """Postal-only fallback with ambiguous (multi-country) postal codes.

    When a postal code exists in multiple countries and there is no
    suggested_country_code to disambiguate, all output fields remain None.
    """

    @pytest.mark.parametrize(
        "postal, country, desc",
        POSTAL_ONLY_AMBIGUOUS_SAMPLES,
        ids=[s[2] for s in POSTAL_ONLY_AMBIGUOUS_SAMPLES],
    )
    def test_all_fields_none(self, postal, country, desc):
        """[Negative] Ambiguous postal → all output fields are None."""
        result = lookup(_state(postal, country))
        report(f"ambiguous [{desc}]", {
            "postal_town_candidate": result["postal_town_candidate"],
            "postal_lookup_method": result["postal_lookup_method"],
        })
        assert result["postal_town_candidate"] is None
        assert result["postal_admin1_code"] is None
        assert result["postal_region"] is None
        assert result["postal_lookup_method"] is None
