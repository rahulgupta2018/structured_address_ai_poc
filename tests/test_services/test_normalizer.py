"""Tests for normalizer service (Step 0) — text preprocessing and normalization."""

from __future__ import annotations

from services.normalizer import (
    build_raw_address,
    casefold,
    collapse_whitespace,
    extract_ngrams,
    normalize_for_matching,
    normalize_punctuation,
    normalize_unicode,
    preprocess,
    redact_pii,
    to_ascii,
    tokenize,
)

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE ADDRESSES — edit these to test with sample data                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Address used in preprocess() tests — change these to test other inputs
SAMPLE_ADDRESS_1 = "123 Main St"
SAMPLE_ADDRESS_2 = "Apt 4"
SAMPLE_ADDRESS_3 = "Springfield"
SAMPLE_COUNTRY_CODE = "US"

# Expected outputs for the sample above (update when changing addresses)
EXPECTED_RAW = "123 Main St Apt 4 Springfield"
EXPECTED_NORMALIZED = "123 main st apt 4 springfield"

# Strings for normalize_for_matching tests
SAMPLE_UNICODE_TEXT = "  CAFÉ  München  "
EXPECTED_UNICODE_NORMALIZED = "café münchen"

SAMPLE_FULLWIDTH_TEXT = "TOKYO\uff0c JAPAN"
EXPECTED_FULLWIDTH_NORMALIZED = "tokyo, japan"


# ── Pure text transforms ────────────────────────────────────────────────────


class TestNormalizeUnicode:
    def test_nfkc_normalization(self):
        # ﬁ (U+FB01) should decompose to "fi"
        result = normalize_unicode("\ufb01")
        report("normalize_unicode", {"input": "\ufb01", "output": result})
        assert result == "fi"

    def test_plain_ascii_unchanged(self):
        result = normalize_unicode("hello")
        report("normalize_unicode", {"input": "hello", "output": result})
        assert result == "hello"


class TestCollapseWhitespace:
    def test_multiple_spaces(self):
        result = collapse_whitespace("hello   world")
        report("collapse_whitespace", {"input": "hello   world", "output": result})
        assert result == "hello world"

    def test_tabs_and_newlines(self):
        result = collapse_whitespace("a\t\nb")
        report("collapse_whitespace", {"input": "a\\t\\nb", "output": result})
        assert result == "a b"

    def test_leading_trailing(self):
        result = collapse_whitespace("  hello  ")
        report("collapse_whitespace", {"input": "  hello  ", "output": result})
        assert result == "hello"


class TestNormalizePunctuation:
    def test_fullwidth_comma(self):
        assert normalize_punctuation("\uff0c") == ","

    def test_fullwidth_period(self):
        assert normalize_punctuation("\uff0e") == "."

    def test_ideographic_comma(self):
        assert normalize_punctuation("\u3001") == ","

    def test_regular_punctuation_unchanged(self):
        assert normalize_punctuation("hello, world.") == "hello, world."


class TestCasefold:
    def test_upper_to_lower(self):
        result = casefold("HELLO")
        report("casefold", {"input": "HELLO", "output": result})
        assert result == "hello"

    def test_german_eszett(self):
        # ß casefolds to "ss"
        result = casefold("Straße")
        report("casefold", {"input": "Straße", "output": result})
        assert result == "strasse"


class TestToAscii:
    def test_accented_characters(self):
        result = to_ascii("café")
        report("to_ascii", {"input": "café", "output": result})
        assert result == "cafe"

    def test_umlaut(self):
        result = to_ascii("München")
        report("to_ascii", {"input": "München", "output": result})
        assert result == "Munchen"


class TestNormalizeForMatching:
    def test_full_pipeline(self):
        result = normalize_for_matching(SAMPLE_UNICODE_TEXT)
        report("normalize_for_matching", {"input": SAMPLE_UNICODE_TEXT, "output": result})
        assert result == EXPECTED_UNICODE_NORMALIZED

    def test_fullwidth_and_unicode(self):
        result = normalize_for_matching(SAMPLE_FULLWIDTH_TEXT)
        report("normalize_for_matching", {"input": SAMPLE_FULLWIDTH_TEXT, "output": result})
        assert result == EXPECTED_FULLWIDTH_NORMALIZED


# ── Tokenizer & N-grams ─────────────────────────────────────────────────────


class TestTokenize:
    def test_basic(self):
        result = tokenize("hello world")
        report("tokenize", {"input": "hello world", "output": result})
        assert result == ["hello", "world"]

    def test_empty(self):
        result = tokenize("")
        report("tokenize", {"input": "", "output": result})
        assert result == []

    def test_filters_empty_strings(self):
        result = tokenize("a  b")
        report("tokenize", {"input": "a  b", "output": result})
        assert result == ["a", "b"]


class TestExtractNgrams:
    def test_unigrams_and_bigrams(self):
        tokens = ["a", "b", "c"]
        ngrams = extract_ngrams(tokens, min_n=1, max_n=2)
        report("extract_ngrams", {"tokens": tokens, "max_n": 2, "ngrams": ngrams})
        assert "a" in ngrams
        assert "b" in ngrams
        assert "c" in ngrams
        assert "a b" in ngrams
        assert "b c" in ngrams
        assert "a b c" not in ngrams

    def test_max_n_larger_than_tokens(self):
        tokens = ["x", "y"]
        ngrams = extract_ngrams(tokens, min_n=1, max_n=4)
        report("extract_ngrams", {"tokens": tokens, "max_n": 4, "ngrams": ngrams})
        assert "x y" in ngrams
        assert len([ng for ng in ngrams if " " in ng]) == 1

    def test_empty_tokens(self):
        result = extract_ngrams([], min_n=1, max_n=4)
        report("extract_ngrams", {"tokens": [], "output": result})
        assert result == []


# ── Build raw address ───────────────────────────────────────────────────────


class TestBuildRawAddress:
    def test_concatenates_non_empty(self):
        lines = ["Line 1", "Line 2", "Line 3"]
        result = build_raw_address(lines)
        report("build_raw_address", {"input_lines": lines, "output": result})
        assert result == "Line 1 Line 2 Line 3"

    def test_skips_empty_and_none(self):
        lines = ["Line 1", "", None, "Line 3"]
        result = build_raw_address(lines)
        report("build_raw_address", {"input_lines": lines, "output": result})
        assert result == "Line 1 Line 3"

    def test_all_empty(self):
        lines = ["", "", ""]
        result = build_raw_address(lines)
        report("build_raw_address", {"input_lines": lines, "output": result})
        assert result == ""

    def test_strips_whitespace(self):
        lines = ["  Line 1  ", "  Line 2  "]
        result = build_raw_address(lines)
        report("build_raw_address", {"input_lines": lines, "output": result})
        assert result == "Line 1 Line 2"


# ── Redact PII ──────────────────────────────────────────────────────────────


class TestRedactPii:
    def test_long_string(self):
        result = redact_pii("1234567890")
        assert result == "12345…[redacted]"

    def test_short_string(self):
        result = redact_pii("abc")
        assert result == "abc…"

    def test_empty(self):
        assert redact_pii("") == "<empty>"

    def test_none(self):
        assert redact_pii(None) == "<empty>"


# ── Preprocess (state-based) ────────────────────────────────────────────────


class TestPreprocess:
    def test_builds_raw_address(self):
        state = {
            "address_1": SAMPLE_ADDRESS_1,
            "address_2": SAMPLE_ADDRESS_2,
            "address_3": SAMPLE_ADDRESS_3,
        }
        report("preprocess input", state)
        result = preprocess(state)
        report("preprocess output", {"raw_address": result["raw_address"], "normalized": result["normalized"]})
        assert result["raw_address"] == EXPECTED_RAW
        assert result["normalized"] == EXPECTED_NORMALIZED

    def test_handles_none_and_empty(self):
        state = {
            "address_1": SAMPLE_ADDRESS_1,
            "address_2": None,
            "address_3": "",
        }
        report("preprocess input", state)
        result = preprocess(state)
        report("preprocess output", {"raw_address": result["raw_address"]})
        assert result["raw_address"] == SAMPLE_ADDRESS_1

    def test_all_empty_warns(self):
        state = {"address_1": None, "address_2": None, "address_3": None}
        report("preprocess input", state)
        result = preprocess(state)
        report("preprocess output", {"raw_address": result["raw_address"], "warnings": result.get("warnings", [])})
        assert result["raw_address"] == ""
        assert result["normalized"] == ""
        assert "no_address_data" in result.get("warnings", [])

    def test_preserves_existing_state(self):
        state = {
            "address_1": SAMPLE_ADDRESS_1,
            "address_2": None,
            "address_3": None,
            "country_code": SAMPLE_COUNTRY_CODE,
        }
        result = preprocess(state)
        report("preprocess output", {"raw_address": result["raw_address"], "country_code": result["country_code"]})
        assert result["country_code"] == SAMPLE_COUNTRY_CODE
