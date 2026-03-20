"""Tests for mismatch_detector service (Step 4) — country-code mismatch detection.

Validates ``detect(state)`` which cross-checks a town candidate against the
GeoNames database to determine whether the stated country code is plausible.
When the town does NOT exist in the stated country but DOES exist elsewhere,
the function flags ``mismatch_detected=True`` and suggests the highest-population
country.

All tests call the **real** ``detect()`` against the **real** SQLite database —
zero mocks.
"""

from __future__ import annotations

import pytest

from services.libpostal_parser import parse
from services.mismatch_detector import detect
from services.postal_lookup import lookup
from services.geonames_exact import match

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE DATA — edit these to test with your own addresses               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# ── Mismatch samples: town exists in OTHER countries, NOT in stated CC ──────
# (town, wrong_country_code, expected_suggested_cc)
MISMATCH_SAMPLES = [
    ("Ko Lanta",      "US", "TH"),  # Thai town, only in TH
    ("Bari Sardo",    "IE", "IT"),  # Italian town, only in IT
    ("Mumbai",        "US", "IN"),  # Indian mega-city, only in IN
    ("Tokyo",         "US", "JP"),  # Japanese capital, only in JP
    ("London",        "JP", "GB"),  # Multi-country, but not in JP → highest-pop GB
    ("San Francisco", "AU", "US"),  # Multi-country, but not in AU → highest-pop US
]

# ── No-mismatch samples: town exists in the stated country ──────────────────
# (town, correct_country_code)
NO_MISMATCH_SAMPLES = [
    ("Ko Lanta",    "TH"),
    ("Bari Sardo",  "IT"),
    ("Mumbai",      "IN"),
    ("London",      "GB"),
    ("Springfield", "US"),
]

# ── Multi-country, no mismatch: town in MANY countries including stated CC ──
# (town, country_code_that_also_has_it)
MULTI_COUNTRY_NO_MISMATCH_SAMPLES = [
    ("Springfield", "AU"),  # AU + US + GB
    ("Berlin",      "US"),  # US + DE + SV + ZA + …
    ("Paris",       "US"),  # US + FR + CA + …
]

# ── Exact-match skip samples: detection bypassed when exact_match=True ──────
# (town, country_code)  — detection shouldn't even run
EXACT_MATCH_SKIP_SAMPLES = [
    ("Tokyo",  "US"),   # Would mismatch, but exact_match=True → skip
    ("London", "GB"),   # Wouldn't mismatch, but still skipped
]

# ── No-candidate samples: nothing to look up ────────────────────────────────
# (libpostal_town, town_candidate)
NO_CANDIDATE_SAMPLES = [
    (None,  None),   # Both None
    ("",    None),   # Empty string
    ("  ",  None),   # Whitespace-only
]

# ── Town not in database at all ─────────────────────────────────────────────
NOT_IN_DB_TOWN = "Xyzzyville"
NOT_IN_DB_COUNTRY = "US"

# ── Candidate fallback: libpostal_town=None, uses town_candidate instead ────
# (town_candidate, wrong_country_code, expected_suggested_cc)
CANDIDATE_FALLBACK_SAMPLES = [
    ("Bari Sardo", "IE", "IT"),
    ("Mumbai",     "AU", "IN"),
]

# ── Whitespace sample ───────────────────────────────────────────────────────
WHITESPACE_TOWN = "  Ko Lanta  "
WHITESPACE_WRONG_CC = "US"
WHITESPACE_EXPECTED_CC = "TH"

# ── E2E raw-address samples: parse → lookup → match → detect ────────────────
# (raw_address, country_code, expected_mismatch, expected_suggested_cc)
E2E_MISMATCH_SAMPLES = [
    ("Ko Lanta, 81150",            "US", True,  "TH"),   # Wrong CC, Step 4 detects
    ("Bari Sardo, 08042",          "IE", True,  "IT"),   # Wrong CC, Step 4 detects
    ("Mumbai, Maharashtra",        "US", True,  "IN"),   # Wrong CC, Step 4 detects
]

E2E_NO_MISMATCH_SAMPLES = [
    ("Springfield, IL 62701",      "US", False, None),   # Correct CC, exact match → skip
    ("Berlin, 10115",              "DE", False, None),   # Correct CC, exact match → skip
]

E2E_STEP1_MISMATCH_SAMPLES = [
    ("Tokyo, Japan",               "US", True,  "JP"),   # Step 1 detects country mismatch
    (                                                     # Full Thai address + wrong CC=US:
        "Villa E5, Malee Beach, 541/2 Moo 2, "           #   Step 1 flags mismatch (Thailand≠US)
        "Long Beach Pra-Ae Beach, 81150 Krabi, Thailand", #   Step 3 false-positive "long" → exact_match=True
        "US", True, "TH",                                #   Step 4 skips (exact_match), but Step 1 flag preserved
    ),
]

E2E_NO_MATCH_SAMPLES = [
    ("Taxila, GT Road, Pakistan",  "PK", False, None),   # City not in DB, no mismatch
    (                                                     # Full Pakistan address:
        "Plot 16-B, Punjab Small Industries Estate, "     #   Taxila not in GeoNames DB
        "Jhang Bahtra Road, Taxila, Pakistan",            #   "jhang" stays in street, not candidates
        "PK", False, None,
    ),
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_state(
    town: str | None = None,
    country_code: str = "",
    exact_match: bool = False,
    town_candidate: str | None = None,
    **extra,
) -> dict:
    """Build a minimal state dict for detect()."""
    state = {
        "exact_match": exact_match,
        "libpostal_town": town,
        "country_code": country_code,
    }
    if town_candidate is not None:
        state["town_candidate"] = town_candidate
    state.update(extra)
    return state


def _run_e2e_pipeline(raw_address: str, country_code: str) -> dict:
    """Run Steps 1–4: parse → lookup → match → detect."""
    state = {"raw_address": raw_address, "country_code": country_code}
    parse(state)
    lookup(state)
    match(state)
    detect(state)
    return state


# ── Test classes ─────────────────────────────────────────────────────────────


class TestDetectMismatch:
    """Town exists in other countries but NOT in the stated country → mismatch."""

    @pytest.mark.parametrize(
        "town, wrong_cc, expected_cc",
        MISMATCH_SAMPLES,
        ids=[s[0] for s in MISMATCH_SAMPLES],
    )
    def test_mismatch_detected(self, town, wrong_cc, expected_cc):
        """Town not found in stated CC → mismatch flagged, highest-pop CC suggested."""
        state = _build_state(town=town, country_code=wrong_cc, exact_match=False)
        report(f"detect input ({town}+{wrong_cc})", state)
        result = detect(state)
        report(f"detect output ({town}+{wrong_cc})", {
            "mismatch_detected": result["mismatch_detected"],
            "suggested_country_code": result["suggested_country_code"],
        })
        assert result["mismatch_detected"] is True
        assert result["suggested_country_code"] == expected_cc


class TestDetectNoMismatch:
    """Town exists in the stated country → no mismatch flagged."""

    @pytest.mark.parametrize(
        "town, correct_cc",
        NO_MISMATCH_SAMPLES,
        ids=[s[0] for s in NO_MISMATCH_SAMPLES],
    )
    def test_no_mismatch(self, town, correct_cc):
        """Town found in stated CC → mismatch_detected stays False."""
        state = _build_state(town=town, country_code=correct_cc, exact_match=False)
        report(f"detect input ({town}+{correct_cc})", state)
        result = detect(state)
        report(f"detect output ({town}+{correct_cc})", {
            "mismatch_detected": result["mismatch_detected"],
        })
        assert result["mismatch_detected"] is False
        assert result["suggested_country_code"] is None


class TestDetectMultiCountryNoMismatch:
    """Town exists in many countries INCLUDING the stated one → no mismatch."""

    @pytest.mark.parametrize(
        "town, cc",
        MULTI_COUNTRY_NO_MISMATCH_SAMPLES,
        ids=[f"{s[0]}+{s[1]}" for s in MULTI_COUNTRY_NO_MISMATCH_SAMPLES],
    )
    def test_multi_country_no_mismatch(self, town, cc):
        """Town appears in multiple countries; stated CC is among them → no mismatch."""
        state = _build_state(town=town, country_code=cc, exact_match=False)
        report(f"detect input ({town}+{cc})", state)
        result = detect(state)
        report(f"detect output ({town}+{cc})", {
            "mismatch_detected": result["mismatch_detected"],
        })
        assert result["mismatch_detected"] is False
        assert result["suggested_country_code"] is None


class TestDetectExactMatchSkips:
    """When exact_match=True, detection is bypassed entirely."""

    @pytest.mark.parametrize(
        "town, cc",
        EXACT_MATCH_SKIP_SAMPLES,
        ids=[s[0] for s in EXACT_MATCH_SKIP_SAMPLES],
    )
    def test_exact_match_skips(self, town, cc):
        """exact_match=True → detect() returns immediately, no DB query."""
        state = _build_state(town=town, country_code=cc, exact_match=True)
        report(f"detect input (exact_match, {town}+{cc})", state)
        result = detect(state)
        report("detect output (skipped)", {
            "mismatch_detected": result["mismatch_detected"],
        })
        assert result["mismatch_detected"] is False
        assert result["suggested_country_code"] is None


class TestDetectNoCandidate:
    """No town candidate available → early return, no detection."""

    @pytest.mark.parametrize(
        "libpostal_town, town_candidate",
        NO_CANDIDATE_SAMPLES,
        ids=["both_none", "empty_string", "whitespace_only"],
    )
    def test_no_candidate(self, libpostal_town, town_candidate):
        """Missing or blank candidate → mismatch stays False."""
        state = _build_state(
            town=libpostal_town,
            country_code="US",
            exact_match=False,
            town_candidate=town_candidate,
        )
        report("detect input (no candidate)", state)
        result = detect(state)
        report("detect output (no candidate)", {
            "mismatch_detected": result["mismatch_detected"],
        })
        assert result["mismatch_detected"] is False
        assert result["suggested_country_code"] is None


class TestDetectTownNotInDB:
    """Town not found in any country → no mismatch (nothing to compare)."""

    def test_town_not_in_db(self):
        """Non-existent town returns no results → mismatch stays False."""
        state = _build_state(
            town=NOT_IN_DB_TOWN,
            country_code=NOT_IN_DB_COUNTRY,
            exact_match=False,
        )
        report("detect input (not in DB)", state)
        result = detect(state)
        report("detect output (not in DB)", {
            "mismatch_detected": result["mismatch_detected"],
        })
        assert result["mismatch_detected"] is False
        assert result["suggested_country_code"] is None


class TestDetectCandidateFallback:
    """libpostal_town is None — falls back to town_candidate."""

    @pytest.mark.parametrize(
        "town_candidate, wrong_cc, expected_cc",
        CANDIDATE_FALLBACK_SAMPLES,
        ids=[s[0] for s in CANDIDATE_FALLBACK_SAMPLES],
    )
    def test_candidate_fallback(self, town_candidate, wrong_cc, expected_cc):
        """town_candidate used when libpostal_town is None → mismatch detected."""
        state = _build_state(
            town=None,
            country_code=wrong_cc,
            exact_match=False,
            town_candidate=town_candidate,
        )
        report(f"detect input (fallback {town_candidate}+{wrong_cc})", state)
        result = detect(state)
        report(f"detect output (fallback {town_candidate}+{wrong_cc})", {
            "mismatch_detected": result["mismatch_detected"],
            "suggested_country_code": result["suggested_country_code"],
        })
        assert result["mismatch_detected"] is True
        assert result["suggested_country_code"] == expected_cc


class TestDetectWhitespace:
    """Whitespace in town name is handled correctly."""

    def test_whitespace_trimmed(self):
        """Leading/trailing spaces stripped by list_countries_for_city → correct mismatch."""
        state = _build_state(
            town=WHITESPACE_TOWN,
            country_code=WHITESPACE_WRONG_CC,
            exact_match=False,
        )
        report("detect input (whitespace)", state)
        result = detect(state)
        report("detect output (whitespace)", {
            "mismatch_detected": result["mismatch_detected"],
            "suggested_country_code": result["suggested_country_code"],
        })
        assert result["mismatch_detected"] is True
        assert result["suggested_country_code"] == WHITESPACE_EXPECTED_CC


class TestDetectPreservesState:
    """Pre-existing state keys survive after detect()."""

    def test_extra_keys_preserved(self):
        """detect() should not remove unrelated keys from state."""
        state = _build_state(
            town="Ko Lanta",
            country_code="TH",
            exact_match=False,
            extra_key="preserved",
            row_index=42,
            job_id="abc-123",
        )
        result = detect(state)
        assert result["extra_key"] == "preserved"
        assert result["row_index"] == 42
        assert result["job_id"] == "abc-123"


class TestDetectFromRawAddress:
    """E2E: raw address → parse() → lookup() → match() → detect().

    Simulates the real pipeline flow (Steps 1 → 2 → 3 → 4) to verify
    mismatch detection in realistic scenarios.
    """

    @pytest.mark.parametrize(
        "raw_address, cc, expect_mismatch, expect_suggested",
        E2E_MISMATCH_SAMPLES,
        ids=[s[0].split(",")[0] for s in E2E_MISMATCH_SAMPLES],
    )
    def test_e2e_mismatch(self, raw_address, cc, expect_mismatch, expect_suggested):
        """Wrong country code → Step 4 detects mismatch after full pipeline."""
        state = _run_e2e_pipeline(raw_address, cc)
        report(f"E2E detect ({raw_address}+{cc})", {
            "exact_match": state.get("exact_match"),
            "libpostal_town": state.get("libpostal_town"),
            "town_candidate": state.get("town_candidate"),
            "mismatch_detected": state.get("mismatch_detected"),
            "suggested_country_code": state.get("suggested_country_code"),
        })
        assert state["mismatch_detected"] is expect_mismatch
        assert state["suggested_country_code"] == expect_suggested

    @pytest.mark.parametrize(
        "raw_address, cc, expect_mismatch, expect_suggested",
        E2E_NO_MISMATCH_SAMPLES,
        ids=[s[0].split(",")[0] for s in E2E_NO_MISMATCH_SAMPLES],
    )
    def test_e2e_no_mismatch(self, raw_address, cc, expect_mismatch, expect_suggested):
        """Correct country code + exact match → Step 4 skips, no mismatch."""
        state = _run_e2e_pipeline(raw_address, cc)
        report(f"E2E detect ({raw_address}+{cc})", {
            "exact_match": state.get("exact_match"),
            "mismatch_detected": state.get("mismatch_detected"),
            "suggested_country_code": state.get("suggested_country_code"),
        })
        assert state["exact_match"] is True
        assert state["mismatch_detected"] is expect_mismatch
        assert state["suggested_country_code"] == expect_suggested

    @pytest.mark.parametrize(
        "raw_address, cc, expect_mismatch, expect_suggested",
        E2E_STEP1_MISMATCH_SAMPLES,
        ids=[s[0].split(",")[0] for s in E2E_STEP1_MISMATCH_SAMPLES],
    )
    def test_e2e_step1_mismatch_preserved(self, raw_address, cc, expect_mismatch, expect_suggested):
        """Step 1 detects country mismatch (via libpostal country name vs stated CC).
        Step 4 has no candidate but preserves Step 1's flags via setdefault."""
        state = _run_e2e_pipeline(raw_address, cc)
        report(f"E2E detect ({raw_address}+{cc})", {
            "exact_match": state.get("exact_match"),
            "libpostal_town": state.get("libpostal_town"),
            "mismatch_detected": state.get("mismatch_detected"),
            "suggested_country_code": state.get("suggested_country_code"),
        })
        assert state["mismatch_detected"] is expect_mismatch
        assert state["suggested_country_code"] == expect_suggested

    @pytest.mark.parametrize(
        "raw_address, cc, expect_mismatch, expect_suggested",
        E2E_NO_MATCH_SAMPLES,
        ids=[s[0].split(",")[0] for s in E2E_NO_MATCH_SAMPLES],
    )
    def test_e2e_no_match(self, raw_address, cc, expect_mismatch, expect_suggested):
        """City not in database at all → no exact match, no mismatch."""
        state = _run_e2e_pipeline(raw_address, cc)
        report(f"E2E detect ({raw_address}+{cc})", {
            "exact_match": state.get("exact_match"),
            "libpostal_town": state.get("libpostal_town"),
            "mismatch_detected": state.get("mismatch_detected"),
            "suggested_country_code": state.get("suggested_country_code"),
        })
        assert state["exact_match"] is False
        assert state["mismatch_detected"] is expect_mismatch
        assert state["suggested_country_code"] == expect_suggested
