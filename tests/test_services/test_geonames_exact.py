"""Tests for geonames_exact service (Step 3) — real DB lookups, zero mocks.

Every test calls the real match() and _disambiguate() functions which query the
GeoNames SQLite city-names index. Sample cities and their expected results
(discovered from actual database output) are defined as top-level constants.

Requires: GeoNames SQLite database at the configured path.
"""

from __future__ import annotations

import pytest

from services.geonames_exact import _disambiguate, match
from services.geonames_repo import resolve_all_cities_by_name
from services.libpostal_parser import parse
from services.normalizer import normalize_for_matching
from services.postal_lookup import lookup
from utils.config import CONFIDENCE_EXACT_ALTERNATE, CONFIDENCE_EXACT_PRIMARY

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE DATA & EXPECTED RESULTS — edit here, not inside tests           ║
# ║  Expected values were discovered by running each city through the real  ║
# ║  GeoNames database. If you change the DB, re-run discovery and update   ║
# ║  these constants.                                                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# ── Disambiguate samples: multiple cities sharing the same name ───────────
#    Real Springfield matches from the DB (sorted by population desc)
SPRINGFIELD_MO_ID   = 4409896
SPRINGFIELD_MO_POP  = 170188
SPRINGFIELD_MA_ID   = 4951788
SPRINGFIELD_MA_POP  = 154341
SPRINGFIELD_IL_ID   = 4250542
SPRINGFIELD_IL_POP  = 114394
SPRINGFIELD_OR_ID   = 5754005
SPRINGFIELD_OR_POP  = 60870

# ── Happy-path _disambiguate() samples ────────────────────────────────────
#    (postal_admin1_code, postal_region, expected_geonameid, desc)
DISAMBIGUATE_HAPPY_SAMPLES = [
    ("IL", "Illinois",      SPRINGFIELD_IL_ID, "admin1=IL selects Springfield IL"),
    ("MA", "Massachusetts", SPRINGFIELD_MA_ID, "admin1=MA selects Springfield MA"),
    (None, None,            SPRINGFIELD_MO_ID, "no postal signal → population fallback (MO)"),
]

# ── Negative _disambiguate() samples ─────────────────────────────────────
#    (matches_list, postal_admin1_code, postal_region, expected_result, desc)
DISAMBIGUATE_NEGATIVE_SAMPLES = [
    ("unknown_admin1",),  # unknown admin1 → population fallback
    ("none_admin1",),     # None admin1 explicitly → population fallback
]

# ── Happy-path match() samples ───────────────────────────────────────────
#    (city_name, country, postal_admin1, postal_region, exp_geonameid,
#     exp_town, exp_match_type, exp_confidence, desc)
MATCH_HAPPY_SAMPLES = [
    ("Springfield", "US", "IL", "Illinois",  SPRINGFIELD_IL_ID,
     "Springfield", "primary", CONFIDENCE_EXACT_PRIMARY,
     "Springfield US disambiguated by admin1=IL"),
    ("Berlin",      "DE", "BE", "Berlin",    2950159,
     "Berlin",      "primary", CONFIDENCE_EXACT_PRIMARY,
     "Berlin DE unique city"),
    ("San Francisco", "US", "CA", "California", 5391959,
     "San Francisco", "primary", CONFIDENCE_EXACT_PRIMARY,
     "San Francisco US unique city"),
    ("Ko Lanta",    "TH", "81", "Krabi",     1152670,
     "Ko Lanta",    "primary", CONFIDENCE_EXACT_PRIMARY,
     "Ko Lanta TH unique city"),
    ("Bari Sardo",  "IT", "14", "Sardegna",  2525595,
     "Bari Sardo",  "primary", CONFIDENCE_EXACT_PRIMARY,
     "Bari Sardo IT postal fallback candidate"),
]

# ── Alternate name sample ────────────────────────────────────────────────
ALT_NAME_INPUT       = "Morbi"
ALT_NAME_PRIMARY     = "Morvi"
ALT_NAME_COUNTRY     = "IN"
ALT_NAME_GEONAMEID   = 1262775
ALT_NAME_CONFIDENCE  = CONFIDENCE_EXACT_ALTERNATE

# ── Negative match() samples ─────────────────────────────────────────────
#    (state_dict, desc)
MATCH_NO_RESULT_SAMPLES = [
    (
        {
            "libpostal_town": "Xyzzyville",
            "libpostal_city_candidates": ["Xyzzyville"],
            "postal_town_candidate": None,
            "postal_admin1_code": None,
            "postal_region": None,
            "country_code": "US",
        },
        "non-existent city Xyzzyville",
    ),
    (
        {"country_code": "US"},
        "empty state with no candidates",
    ),
]

# ── Candidate fallback sample ────────────────────────────────────────────
FALLBACK_LIBPOSTAL_TOWN     = "Xyzzyville"
FALLBACK_POSTAL_CANDIDATE   = "Springfield"
FALLBACK_ADMIN1             = "IL"
FALLBACK_REGION             = "Illinois"
FALLBACK_COUNTRY            = "US"
FALLBACK_EXPECTED_ID        = SPRINGFIELD_IL_ID
FALLBACK_EXPECTED_TOWN      = "Springfield"

# ── Preserve-state sample ────────────────────────────────────────────────
PRESERVE_CITY        = "Berlin"
PRESERVE_COUNTRY     = "DE"
PRESERVE_EXTRA_KEY   = "preserved_value"
PRESERVE_ROW_INDEX   = 77
PRESERVE_JOB_ID      = "test-batch-step3"

# ── Raw-address end-to-end samples: raw → parse() → lookup() → match() ──
#    (raw_address, country_code, exp_exact_match, exp_town, exp_geonameid,
#     exp_match_type, exp_confidence, desc)
RAW_ADDRESS_SAMPLES = [
    (
        "Via Roma 15, 08042 Barisardo, Italy",
        "IT", True, "Bari Sardo", 2525595, "primary", CONFIDENCE_EXACT_PRIMARY,
        "Italy with postal 08042 → Bari Sardo via postal fallback",
    ),
    (
        "Villa E5, Malee Beach, 541/2 Moo 2, "
        "Long Beach Pra-Ae Beach, 81150 Krabi, Thailand",
        "TH", True, "Krabi", 1152633, "primary", CONFIDENCE_EXACT_PRIMARY,
        "Thailand with postal 81150 → Krabi via libpostal_town",
    ),
    (
        "123 Main St, Springfield, IL 62701, USA",
        "US", True, "Springfield", SPRINGFIELD_IL_ID, "primary", CONFIDENCE_EXACT_PRIMARY,
        "US Springfield with postal 62701 → disambiguated to IL",
    ),
    (
        "Plot 16-B, Punjab Small Industries Estate, "
        "Jhang Bahtra Road, Taxila, Pakistan",
        "PK", True, "Taxila", None, "postal", CONFIDENCE_EXACT_ALTERNATE,
        "Pakistan Taxila — postal fallback (not in cities500)",
    ),
    (
        "1-1 Marunouchi, Chiyoda-ku, Tokyo, Japan",
        "JP", True, "Chiyoda Ku", None, "postal", CONFIDENCE_EXACT_ALTERNATE,
        "Japan Chiyoda-ku — postal fallback (ward-level match)",
    ),
]

# ── Edge-case: wrong country code causes false-positive match ────────────
#    Krabi address (Thailand) submitted with country_code="US".
#    - Step 1: parses town=krabi, candidates=[long, krabi], flags mismatch
#    - Step 2: postal 81150 in US → no match (it's a Thai code)
#    - Step 3: "krabi" not in US DB, but "long" (from "Long Beach Pra-Ae
#      Beach") accidentally matches a US city as an alternate name!
#    With suggested-CC-first logic, Step 3 now tries TH before US.
#    "krabi" matches in TH → resolves correctly. No false positive.
WRONG_CC_RAW = (
    "Villa E5, Malee Beach, 541/2 Moo 2, "
    "Long Beach Pra-Ae Beach, 81150 Krabi, Thailand"
)
WRONG_CC_COUNTRY             = "US"
WRONG_CC_EXPECTED_MATCH      = True       # "krabi" matches in TH (suggested CC)
WRONG_CC_EXPECTED_TOWN       = "Krabi"    # correct city in suggested country
WRONG_CC_EXPECTED_GEONAMEID  = 1152633    # TH city
WRONG_CC_EXPECTED_TYPE       = "primary"
WRONG_CC_EXPECTED_CONFIDENCE = CONFIDENCE_EXACT_PRIMARY

# ── Edge-case: city not in GeoNames DB ───────────────────────────────────
#    Taxila is a real city in Pakistan but absent from GeoNames DB.
#    libpostal correctly parses town=taxila, but Step 3 can't find it.
#    The street "Jhang Bahtra Road" contains "Jhang" which is a real
#    Pakistani city — but libpostal correctly assigns it to the street
#    component, NOT as a city candidate. Step 3 only tries
#    libpostal_city_candidates, so "Jhang" is never searched.
#    This is the correct behaviour — Step 3 should NOT match road-name
#    tokens as cities. The scanner (Step 5) might pick it up later.
CITY_NOT_IN_DB_RAW = (
    "Plot 16-B, Punjab Small Industries Estate, "
    "Jhang Bahtra Road, Taxila, Pakistan"
)
CITY_NOT_IN_DB_COUNTRY       = "PK"
CITY_NOT_IN_DB_EXPECTED_MATCH = True   # now resolves via postal fallback
CITY_NOT_IN_DB_EXPECTED_TOWN  = "Taxila"


# ═══════════════════════════════════════════════════════════════════════════
#  Helper — build a state dict for match()
# ═══════════════════════════════════════════════════════════════════════════

def _state(
    city: str,
    country: str,
    postal_admin1: str | None = None,
    postal_region: str | None = None,
    postal_town: str | None = None,
) -> dict:
    """Build a minimal state dict for match().

    Sets libpostal_town and libpostal_city_candidates to [city], and
    optionally fills in postal disambiguation signals.
    """
    return {
        "libpostal_town": city,
        "libpostal_city_candidates": [city] if city else [],
        "postal_town_candidate": postal_town,
        "postal_admin1_code": postal_admin1,
        "postal_region": postal_region,
        "country_code": country,
    }


def _real_springfields() -> list[dict]:
    """Fetch the real Springfield matches from the DB for _disambiguate() tests."""
    return resolve_all_cities_by_name("US", normalize_for_matching("Springfield"))


# ═══════════════════════════════════════════════════════════════════════════
#  Test Classes — all call the REAL functions against the REAL SQLite DB
# ═══════════════════════════════════════════════════════════════════════════


class TestDisambiguateHappyPath:
    """_disambiguate() with real Springfield matches from the DB.

    Each sample provides a postal admin1 code (or None) and the expected
    winner. When admin1 matches a single city, that city wins. When no
    postal signal is given, the largest city by population wins.
    """

    @pytest.mark.parametrize(
        "admin1, region, exp_id, desc",
        DISAMBIGUATE_HAPPY_SAMPLES,
        ids=[s[3] for s in DISAMBIGUATE_HAPPY_SAMPLES],
    )
    def test_selects_correct_city(self, admin1, region, exp_id, desc):
        """Disambiguation picks the expected city based on admin1 or population."""
        matches = _real_springfields()
        result = _disambiguate(matches, postal_admin1_code=admin1, postal_region=region)
        report(f"disambiguate [{desc}]", {
            "postal_admin1_code": admin1,
            "winner_id": result.get("geonameid"),
            "winner_name": result.get("name"),
            "winner_admin1": result.get("admin1_code"),
        })
        assert result["geonameid"] == exp_id

    def test_single_match_returns_it(self):
        """With only one match, no disambiguation logic needed."""
        matches = _real_springfields()
        single = [m for m in matches if m["admin1_code"] == "IL"][:1]
        result = _disambiguate(single, postal_admin1_code=None, postal_region=None)
        assert result["geonameid"] == SPRINGFIELD_IL_ID


class TestDisambiguateNegative:
    """_disambiguate() negative and edge cases."""

    def test_empty_list_returns_empty_dict(self):
        """[Negative] Empty match list → empty dict."""
        result = _disambiguate([], postal_admin1_code="IL", postal_region=None)
        report("disambiguate empty list", {"result": result})
        assert result == {}

    def test_unknown_admin1_falls_back_to_population(self):
        """[Negative] Unknown admin1 code → falls back to largest population."""
        matches = _real_springfields()
        result = _disambiguate(matches, postal_admin1_code="XX", postal_region=None)
        report("disambiguate unknown admin1", {
            "admin1": "XX",
            "winner_id": result["geonameid"],
        })
        assert result["geonameid"] == SPRINGFIELD_MO_ID

    def test_case_insensitive_admin1(self):
        """[Edge] Admin1 matching is case-insensitive ('il' matches 'IL')."""
        matches = _real_springfields()
        result = _disambiguate(matches, postal_admin1_code="il", postal_region=None)
        assert result["geonameid"] == SPRINGFIELD_IL_ID


class TestMatchHappyPath:
    """match() with valid cities from multiple countries.

    Each sample is a city + country + postal signals tuple. The same generic
    assertions run for every sample — checking exact_match, geonames_id,
    town_candidate, match_type, and match_confidence.
    """

    @pytest.mark.parametrize(
        "city, country, admin1, region, exp_id, exp_town, exp_type, exp_conf, desc",
        MATCH_HAPPY_SAMPLES,
        ids=[s[8] for s in MATCH_HAPPY_SAMPLES],
    )
    def test_exact_match_true(self, city, country, admin1, region, exp_id, exp_town, exp_type, exp_conf, desc):
        """City resolves with exact_match=True."""
        result = match(_state(city, country, admin1, region))
        report(f"match [{desc}]", {
            "exact_match": result["exact_match"],
            "geonames_id": result["geonames_id"],
            "town_candidate": result["town_candidate"],
            "match_type": result["match_type"],
            "match_confidence": result["match_confidence"],
        })
        assert result["exact_match"] is True

    @pytest.mark.parametrize(
        "city, country, admin1, region, exp_id, exp_town, exp_type, exp_conf, desc",
        MATCH_HAPPY_SAMPLES,
        ids=[s[8] for s in MATCH_HAPPY_SAMPLES],
    )
    def test_geonames_id(self, city, country, admin1, region, exp_id, exp_town, exp_type, exp_conf, desc):
        """City resolves to the expected geonames_id."""
        result = match(_state(city, country, admin1, region))
        assert result["geonames_id"] == exp_id

    @pytest.mark.parametrize(
        "city, country, admin1, region, exp_id, exp_town, exp_type, exp_conf, desc",
        MATCH_HAPPY_SAMPLES,
        ids=[s[8] for s in MATCH_HAPPY_SAMPLES],
    )
    def test_town_candidate(self, city, country, admin1, region, exp_id, exp_town, exp_type, exp_conf, desc):
        """City resolves to the expected town_candidate string."""
        result = match(_state(city, country, admin1, region))
        assert result["town_candidate"] == exp_town

    @pytest.mark.parametrize(
        "city, country, admin1, region, exp_id, exp_town, exp_type, exp_conf, desc",
        MATCH_HAPPY_SAMPLES,
        ids=[s[8] for s in MATCH_HAPPY_SAMPLES],
    )
    def test_match_type(self, city, country, admin1, region, exp_id, exp_town, exp_type, exp_conf, desc):
        """City resolves with the expected match_type (primary/ascii/alternate)."""
        result = match(_state(city, country, admin1, region))
        assert result["match_type"] == exp_type

    @pytest.mark.parametrize(
        "city, country, admin1, region, exp_id, exp_town, exp_type, exp_conf, desc",
        MATCH_HAPPY_SAMPLES,
        ids=[s[8] for s in MATCH_HAPPY_SAMPLES],
    )
    def test_match_confidence(self, city, country, admin1, region, exp_id, exp_town, exp_type, exp_conf, desc):
        """City resolves with the expected confidence score."""
        result = match(_state(city, country, admin1, region))
        assert result["match_confidence"] == exp_conf


class TestMatchAlternateName:
    """match() when the input spelling is an alternate name in GeoNames.

    'Morbi' is an alternate spelling for the GeoNames primary name 'Morvi'.
    match() should preserve the input spelling as town_candidate and set
    match_type to 'alternate' with a lower confidence than primary matches.
    """

    def test_alternate_preserves_input_spelling(self):
        """Alternate name 'Morbi' resolves but preserves input spelling (not 'Morvi')."""
        state = _state(ALT_NAME_INPUT, ALT_NAME_COUNTRY)
        result = match(state)
        report("match alternate name", {
            "exact_match": result["exact_match"],
            "geonames_id": result["geonames_id"],
            "town_candidate": result["town_candidate"],
            "match_type": result["match_type"],
            "match_confidence": result["match_confidence"],
        })
        assert result["exact_match"] is True
        assert result["geonames_id"] == ALT_NAME_GEONAMEID
        assert result["town_candidate"] == ALT_NAME_INPUT
        assert result["match_type"] == "alternate"
        assert result["match_confidence"] == ALT_NAME_CONFIDENCE


class TestMatchNoResults:
    """match() when no city is found in the database.

    Covers non-existent city names and empty state (no candidates at all).
    All output fields stay at their defaults.
    """

    @pytest.mark.parametrize(
        "state, desc",
        MATCH_NO_RESULT_SAMPLES,
        ids=[s[1] for s in MATCH_NO_RESULT_SAMPLES],
    )
    def test_no_match_defaults(self, state, desc):
        """[Negative] No DB match → exact_match=False, geonames_id=None."""
        result = match(dict(state))
        report(f"no-match [{desc}]", {
            "exact_match": result["exact_match"],
            "geonames_id": result["geonames_id"],
            "town_candidate": result["town_candidate"],
            "match_confidence": result["match_confidence"],
        })
        assert result["exact_match"] is False
        assert result["geonames_id"] is None
        assert result["town_candidate"] is None
        assert result["match_type"] is None
        assert result["match_confidence"] == 0.0


class TestMatchCandidateFallback:
    """match() falls back to postal_town_candidate when libpostal_town has no match.

    The candidate order is: libpostal_town → other libpostal_city_candidates →
    postal_town_candidate. If the first candidate fails, the next is tried.
    """

    def test_fallback_to_postal_candidate(self):
        """[Edge] libpostal_town='Xyzzyville' has no match, but postal_town_candidate='Springfield' does."""
        state = _state(
            FALLBACK_LIBPOSTAL_TOWN, FALLBACK_COUNTRY,
            postal_admin1=FALLBACK_ADMIN1, postal_region=FALLBACK_REGION,
            postal_town=FALLBACK_POSTAL_CANDIDATE,
        )
        result = match(state)
        report("candidate fallback", {
            "libpostal_town": FALLBACK_LIBPOSTAL_TOWN,
            "postal_town_candidate": FALLBACK_POSTAL_CANDIDATE,
            "exact_match": result["exact_match"],
            "geonames_id": result["geonames_id"],
            "town_candidate": result["town_candidate"],
        })
        assert result["exact_match"] is True
        assert result["geonames_id"] == FALLBACK_EXPECTED_ID
        assert result["town_candidate"] == FALLBACK_EXPECTED_TOWN


class TestMatchPreservesState:
    """match() preserves pre-existing keys in the state dict."""

    def test_extra_keys_survive(self):
        """[Edge] Pre-existing keys (extra_key, row_index, job_id) survive match()."""
        state = _state(PRESERVE_CITY, PRESERVE_COUNTRY, postal_admin1="BE")
        state["extra_key"] = PRESERVE_EXTRA_KEY
        state["row_index"] = PRESERVE_ROW_INDEX
        state["job_id"] = PRESERVE_JOB_ID
        result = match(state)
        report("preserves state", {
            "extra_key": result["extra_key"],
            "row_index": result["row_index"],
            "job_id": result["job_id"],
            "exact_match": result["exact_match"],
        })
        assert result["extra_key"] == PRESERVE_EXTRA_KEY
        assert result["row_index"] == PRESERVE_ROW_INDEX
        assert result["job_id"] == PRESERVE_JOB_ID
        assert result["exact_match"] is True


class TestMatchWrongCountryCode:
    """match() when the country_code is incorrect — suggested-CC-first resolution.

    The Krabi/Thailand address is submitted with country_code="US".
    Step 1 parses town=krabi and candidates=[long, krabi], and flags the
    mismatch (suggested_country_code=TH). Step 2 can't find Thai postal
    81150 in US. Step 3 now tries TH first (suggested CC):
      - "krabi" → matches Krabi in TH (primary, id=1152633)

    This is the correct behaviour: Step 3 tries the suggested country
    first when mismatch_detected is set, avoiding the old false-positive
    where "long" accidentally matched a US city.
    """

    def test_wrong_cc_false_positive(self):
        """[Edge] Thai address + US country → 'Krabi' correctly matched in TH via suggested CC."""
        state = parse({"raw_address": WRONG_CC_RAW, "country_code": WRONG_CC_COUNTRY})
        state = lookup(state)
        result = match(state)
        report("wrong country code (Krabi/US)", {
            "libpostal_town": state.get("libpostal_town"),
            "libpostal_city_candidates": state.get("libpostal_city_candidates"),
            "mismatch_detected": state.get("mismatch_detected"),
            "suggested_country_code": state.get("suggested_country_code"),
            "exact_match": result["exact_match"],
            "geonames_id": result["geonames_id"],
            "town_candidate": result["town_candidate"],
            "match_type": result["match_type"],
        })
        # Step 3 finds a match in suggested CC (TH) — correct resolution
        assert result["exact_match"] is WRONG_CC_EXPECTED_MATCH
        assert result["geonames_id"] == WRONG_CC_EXPECTED_GEONAMEID
        assert result["town_candidate"] == WRONG_CC_EXPECTED_TOWN
        assert result["match_type"] == WRONG_CC_EXPECTED_TYPE
        assert result["match_confidence"] == WRONG_CC_EXPECTED_CONFIDENCE

    def test_wrong_cc_mismatch_flag_preserved(self):
        """[Edge] Mismatch flag from Step 1 survives through Steps 2-3 for Step 7 to use."""
        state = parse({"raw_address": WRONG_CC_RAW, "country_code": WRONG_CC_COUNTRY})
        state = lookup(state)
        result = match(state)
        assert result.get("mismatch_detected") is True
        assert result.get("suggested_country_code") == "TH"


class TestMatchCityNotInDB:
    """match() when the real city is absent from the cities500 database
    but present in the GeoNames postal-codes dataset.

    Taxila is a real Pakistani city not indexed in geonames_city_names
    (too small for cities500.txt). The street name "Jhang Bahtra Road"
    contains "Jhang" (a real PK city), but libpostal correctly assigns
    it to the street component — it never appears in city_candidates.

    Step 3 now has a postal-code fallback: after the geonames_city_names
    lookup fails, it tries the candidate against the postal-codes table
    and finds "Taxila" (postcodes 47050, 47070, 47080 in PK).
    """

    def test_city_not_in_db_postal_fallback(self):
        """[Edge] Taxila not in city_names but found via postal fallback → exact_match=True."""
        state = parse({"raw_address": CITY_NOT_IN_DB_RAW, "country_code": CITY_NOT_IN_DB_COUNTRY})
        state = lookup(state)
        result = match(state)
        report("city not in DB (Taxila/PK)", {
            "libpostal_town": state.get("libpostal_town"),
            "libpostal_city_candidates": state.get("libpostal_city_candidates"),
            "libpostal_street": state.get("libpostal_street"),
            "exact_match": result["exact_match"],
            "geonames_id": result["geonames_id"],
            "town_candidate": result["town_candidate"],
        })
        assert result["exact_match"] is CITY_NOT_IN_DB_EXPECTED_MATCH
        assert result["town_candidate"] == CITY_NOT_IN_DB_EXPECTED_TOWN
        assert result["geonames_id"] is None  # postal fallback has no geonameid
        assert result["match_type"] == "postal"
        assert result["match_confidence"] == CONFIDENCE_EXACT_ALTERNATE

    def test_road_token_not_in_candidates(self):
        """[Edge] 'Jhang' from street name is NOT in libpostal_city_candidates — Step 3 can't see it."""
        state = parse({"raw_address": CITY_NOT_IN_DB_RAW, "country_code": CITY_NOT_IN_DB_COUNTRY})
        report("road token check", {
            "libpostal_street": state.get("libpostal_street"),
            "libpostal_city_candidates": state.get("libpostal_city_candidates"),
        })
        # "jhang" should be in the street, NOT in city candidates
        assert "jhang" in (state.get("libpostal_street") or "").lower()
        candidates = [c.lower() for c in (state.get("libpostal_city_candidates") or [])]
        assert "jhang" not in candidates


class TestMatchFromRawAddress:
    """End-to-end: raw address → parse() (Step 1) → lookup() (Step 2) → match() (Step 3).

    Simulates the real pipeline flow. The raw address is first parsed by
    libpostal, then runs through postal lookup for disambiguation signals,
    and finally through exact matching. Addresses with postal codes that
    resolve cities should get exact_match=True; addresses without postal
    codes for cities not in the DB should get exact_match=False.
    """

    @pytest.mark.parametrize(
        "raw, country, exp_match, exp_town, exp_id, exp_type, exp_conf, desc",
        RAW_ADDRESS_SAMPLES,
        ids=[s[7] for s in RAW_ADDRESS_SAMPLES],
    )
    def test_exact_match_flag(self, raw, country, exp_match, exp_town, exp_id, exp_type, exp_conf, desc):
        """Raw address → full pipeline resolves expected exact_match flag."""
        state = parse({"raw_address": raw, "country_code": country})
        state = lookup(state)
        result = match(state)
        report(f"e2e [{desc}]", {
            "libpostal_town": state.get("libpostal_town"),
            "postal_town_candidate": state.get("postal_town_candidate"),
            "exact_match": result["exact_match"],
            "geonames_id": result["geonames_id"],
            "town_candidate": result["town_candidate"],
            "match_type": result["match_type"],
        })
        assert result["exact_match"] is exp_match

    @pytest.mark.parametrize(
        "raw, country, exp_match, exp_town, exp_id, exp_type, exp_conf, desc",
        RAW_ADDRESS_SAMPLES,
        ids=[s[7] for s in RAW_ADDRESS_SAMPLES],
    )
    def test_town_candidate(self, raw, country, exp_match, exp_town, exp_id, exp_type, exp_conf, desc):
        """Raw address → full pipeline resolves expected town_candidate."""
        state = parse({"raw_address": raw, "country_code": country})
        state = lookup(state)
        result = match(state)
        assert result["town_candidate"] == exp_town

    @pytest.mark.parametrize(
        "raw, country, exp_match, exp_town, exp_id, exp_type, exp_conf, desc",
        RAW_ADDRESS_SAMPLES,
        ids=[s[7] for s in RAW_ADDRESS_SAMPLES],
    )
    def test_geonames_id(self, raw, country, exp_match, exp_town, exp_id, exp_type, exp_conf, desc):
        """Raw address → full pipeline resolves expected geonames_id."""
        state = parse({"raw_address": raw, "country_code": country})
        state = lookup(state)
        result = match(state)
        assert result["geonames_id"] == exp_id

    @pytest.mark.parametrize(
        "raw, country, exp_match, exp_town, exp_id, exp_type, exp_conf, desc",
        RAW_ADDRESS_SAMPLES,
        ids=[s[7] for s in RAW_ADDRESS_SAMPLES],
    )
    def test_match_confidence(self, raw, country, exp_match, exp_town, exp_id, exp_type, exp_conf, desc):
        """Raw address → full pipeline resolves expected confidence."""
        state = parse({"raw_address": raw, "country_code": country})
        state = lookup(state)
        result = match(state)
        assert result["match_confidence"] == exp_conf
