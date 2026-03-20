"""Tests for normalizer service (Step 0) — text preprocessing and normalization.

Every test calls the REAL service function (no mocks). Sample data and expected
results are declared as top-level constants — edit them here, not inside tests.
"""

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
# ║  SAMPLE DATA & EXPECTED RESULTS — edit here, not inside tests            ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# ── Preprocess / full pipeline ──────────────────────────────────────────────
SAMPLE_ADDRESS_1 = "123 Main St"
SAMPLE_ADDRESS_2 = "Apt 4"
SAMPLE_ADDRESS_3 = "Springfield"
SAMPLE_COUNTRY_CODE = "US"

EXPECTED_RAW = "123 Main St Apt 4 Springfield"
EXPECTED_NORMALIZED = "123 main st apt 4 springfield"

# ── normalize_unicode ──────────────────────────────────────────────────────
UNICODE_LIGATURE_INPUT = "\ufb01"          # ﬁ (U+FB01)
UNICODE_LIGATURE_EXPECTED = "fi"
UNICODE_ASCII_INPUT = "hello"
UNICODE_ASCII_EXPECTED = "hello"

# ── collapse_whitespace ───────────────────────────────────────────────────
WHITESPACE_MULTI_INPUT = "hello   world"
WHITESPACE_MULTI_EXPECTED = "hello world"
WHITESPACE_TABS_INPUT = "a\t\nb"
WHITESPACE_TABS_EXPECTED = "a b"
WHITESPACE_TRIM_INPUT = "  hello  "
WHITESPACE_TRIM_EXPECTED = "hello"

# ── normalize_punctuation ────────────────────────────────────────────────
PUNCT_FULLWIDTH_COMMA = "\uff0c"
PUNCT_FULLWIDTH_COMMA_EXPECTED = ","
PUNCT_FULLWIDTH_PERIOD = "\uff0e"
PUNCT_FULLWIDTH_PERIOD_EXPECTED = "."
PUNCT_IDEOGRAPHIC_COMMA = "\u3001"
PUNCT_IDEOGRAPHIC_COMMA_EXPECTED = ","
PUNCT_REGULAR_INPUT = "hello, world."
PUNCT_REGULAR_EXPECTED = "hello, world."

# ── casefold ─────────────────────────────────────────────────────────────
CASEFOLD_UPPER_INPUT = "HELLO"
CASEFOLD_UPPER_EXPECTED = "hello"
CASEFOLD_ESZETT_INPUT = "Straße"
CASEFOLD_ESZETT_EXPECTED = "strasse"

# ── to_ascii ─────────────────────────────────────────────────────────────
ASCII_ACCENT_INPUT = "café"
ASCII_ACCENT_EXPECTED = "cafe"
ASCII_UMLAUT_INPUT = "München"
ASCII_UMLAUT_EXPECTED = "Munchen"

# ── normalize_for_matching (end-to-end) ──────────────────────────────────
MATCH_UNICODE_INPUT = "  CAFÉ  München  "
MATCH_UNICODE_EXPECTED = "café münchen"
MATCH_FULLWIDTH_INPUT = "TOKYO\uff0c JAPAN"
MATCH_FULLWIDTH_EXPECTED = "tokyo, japan"

# ── tokenize ─────────────────────────────────────────────────────────────
TOKENIZE_BASIC_INPUT = "hello world"
TOKENIZE_BASIC_EXPECTED = ["hello", "world"]
TOKENIZE_EMPTY_INPUT = ""
TOKENIZE_EMPTY_EXPECTED = []
TOKENIZE_DOUBLE_SPACE_INPUT = "a  b"
TOKENIZE_DOUBLE_SPACE_EXPECTED = ["a", "b"]

# ── extract_ngrams ───────────────────────────────────────────────────────
NGRAMS_TOKENS = ["a", "b", "c"]
NGRAMS_MAX_2_MUST_INCLUDE = ["a", "b", "c", "a b", "b c"]
NGRAMS_MAX_2_MUST_EXCLUDE = ["a b c"]
NGRAMS_TWO_TOKENS = ["x", "y"]
NGRAMS_TWO_EXPECTED_BIGRAM = "x y"
# ── casefold (idempotent check) ──────────────────────────────────────────
CASEFOLD_ALREADY_LOWER_INPUT    = "hello world"
CASEFOLD_ALREADY_LOWER_EXPECTED = "hello world"

# ── tokenize (single token) ────────────────────────────────────────────
TOKENIZE_SINGLE_INPUT    = "hello"
TOKENIZE_SINGLE_EXPECTED = ["hello"]
# ── build_raw_address ────────────────────────────────────────────────────
RAW_LINES_FULL = ["Line 1", "Line 2", "Line 3"]
RAW_LINES_FULL_EXPECTED = "Line 1 Line 2 Line 3"
RAW_LINES_GAPS = ["Line 1", "", None, "Line 3"]
RAW_LINES_GAPS_EXPECTED = "Line 1 Line 3"
RAW_LINES_EMPTY = ["", "", ""]
RAW_LINES_EMPTY_EXPECTED = ""
RAW_LINES_PADDED = ["  Line 1  ", "  Line 2  "]
RAW_LINES_PADDED_EXPECTED = "Line 1 Line 2"

# ── redact_pii ───────────────────────────────────────────────────────────
REDACT_LONG_INPUT = "1234567890"
REDACT_LONG_EXPECTED = "12345…[redacted]"
REDACT_SHORT_INPUT = "abc"
REDACT_SHORT_EXPECTED = "abc…"
REDACT_EMPTY_EXPECTED = "<empty>"
REDACT_BOUNDARY_INPUT = "abcde"         # exactly keep_chars=5
REDACT_BOUNDARY_EXPECTED = "abcde…"     # <= keep_chars → short path
REDACT_WHITESPACE_INPUT = "   "
REDACT_WHITESPACE_EXPECTED = "<empty>"

# ── Negative / edge-case data ────────────────────────────────────────────
EMPTY_STRING = ""
WHITESPACE_ONLY = "   \t\n  "

# CJK text — unidecode transliterates to pinyin/romaji
CJK_INPUT = "東京都"
CJK_ASCII_EXPECTED = "Dong Jing Du "    # unidecode output for 東京都

# Arabic text
ARABIC_INPUT = "القاهرة"

# Emoji — unidecode strips or transliterates
EMOJI_INPUT = "123 🏠 Main St"

# Mixed fullwidth in realistic address
PUNCT_MIXED_INPUT = "東京都渋谷区\uff0c 150-0002"
PUNCT_MIXED_EXPECTED = "東京都渋谷区, 150-0002"

# build_raw_address — whitespace-only lines should be treated as empty
RAW_LINES_WHITESPACE_ONLY = ["   ", "\t", "  \n  "]
RAW_LINES_WHITESPACE_ONLY_EXPECTED = ""
RAW_LINES_EMPTY_LIST: list[str] = []
RAW_LINES_EMPTY_LIST_EXPECTED = ""

# extract_ngrams — single token
NGRAMS_SINGLE_TOKEN = ["hello"]
NGRAMS_SINGLE_EXPECTED = ["hello"]      # only unigram, no bigrams

# ── Expected warning strings (produced by preprocess()) ─────────────────────
WARN_NO_ADDRESS_DATA = "no_address_data"

# preprocess — whitespace-only address lines → treated as no data
PREPROCESS_WHITESPACE_STATE = {
    "address_1": "   ",
    "address_2": "\t",
    "address_3": "  \n  ",
}
# preprocess — keys missing entirely from state
PREPROCESS_MISSING_KEYS_STATE: dict = {}


# ── Pure text transforms ────────────────────────────────────────────────────


class TestNormalizeUnicode:
    """NFKC unicode normalization — decomposes ligatures and compatibility chars."""

    def test_nfkc_normalization(self):
        """Ligature ﬁ (U+FB01) should decompose to two chars 'fi'."""
        result = normalize_unicode(UNICODE_LIGATURE_INPUT)
        report("normalize_unicode", {"input": UNICODE_LIGATURE_INPUT, "output": result})
        assert result == UNICODE_LIGATURE_EXPECTED

    def test_plain_ascii_unchanged(self):
        """Plain ASCII text passes through NFKC unchanged."""
        result = normalize_unicode(UNICODE_ASCII_INPUT)
        report("normalize_unicode", {"input": UNICODE_ASCII_INPUT, "output": result})
        assert result == UNICODE_ASCII_EXPECTED

    def test_empty_string(self):
        """[Negative] Empty string returns empty without error."""
        result = normalize_unicode(EMPTY_STRING)
        report("normalize_unicode [negative]", {"input": repr(EMPTY_STRING), "output": result})
        assert result == ""


class TestCollapseWhitespace:
    """Collapse runs of whitespace to a single space and strip edges."""

    def test_multiple_spaces(self):
        """Consecutive spaces between words collapse to one space."""
        result = collapse_whitespace(WHITESPACE_MULTI_INPUT)
        report("collapse_whitespace", {"input": WHITESPACE_MULTI_INPUT, "output": result})
        assert result == WHITESPACE_MULTI_EXPECTED

    def test_tabs_and_newlines(self):
        """Tab and newline characters are treated as whitespace and collapsed."""
        result = collapse_whitespace(WHITESPACE_TABS_INPUT)
        report("collapse_whitespace", {"input": repr(WHITESPACE_TABS_INPUT), "output": result})
        assert result == WHITESPACE_TABS_EXPECTED

    def test_leading_trailing(self):
        """Leading and trailing spaces are stripped entirely."""
        result = collapse_whitespace(WHITESPACE_TRIM_INPUT)
        report("collapse_whitespace", {"input": WHITESPACE_TRIM_INPUT, "output": result})
        assert result == WHITESPACE_TRIM_EXPECTED

    def test_empty_string(self):
        """[Negative] Empty string returns empty without error."""
        result = collapse_whitespace(EMPTY_STRING)
        report("collapse_whitespace [negative]", {"input": repr(EMPTY_STRING), "output": result})
        assert result == ""

    def test_whitespace_only(self):
        """[Negative] String of only whitespace/tabs/newlines collapses to empty."""
        result = collapse_whitespace(WHITESPACE_ONLY)
        report("collapse_whitespace [negative]", {"input": repr(WHITESPACE_ONLY), "output": result})
        assert result == ""


class TestNormalizePunctuation:
    """Convert full-width CJK punctuation to ASCII equivalents."""

    def test_fullwidth_comma(self):
        """Full-width comma (U+FF0C) converts to ASCII comma."""
        assert normalize_punctuation(PUNCT_FULLWIDTH_COMMA) == PUNCT_FULLWIDTH_COMMA_EXPECTED

    def test_fullwidth_period(self):
        """Full-width period (U+FF0E) converts to ASCII period."""
        assert normalize_punctuation(PUNCT_FULLWIDTH_PERIOD) == PUNCT_FULLWIDTH_PERIOD_EXPECTED

    def test_ideographic_comma(self):
        """Ideographic comma (U+3001) used in Japanese converts to ASCII comma."""
        assert normalize_punctuation(PUNCT_IDEOGRAPHIC_COMMA) == PUNCT_IDEOGRAPHIC_COMMA_EXPECTED

    def test_regular_punctuation_unchanged(self):
        """Standard ASCII commas and periods pass through unchanged."""
        assert normalize_punctuation(PUNCT_REGULAR_INPUT) == PUNCT_REGULAR_EXPECTED

    def test_empty_string(self):
        """[Negative] Empty string returns empty without error."""
        assert normalize_punctuation(EMPTY_STRING) == ""

    def test_mixed_fullwidth_in_address(self):
        """[Edge] Realistic Japanese address with fullwidth comma amid CJK text."""
        result = normalize_punctuation(PUNCT_MIXED_INPUT)
        report("normalize_punctuation [edge]", {"input": PUNCT_MIXED_INPUT, "output": result})
        assert result == PUNCT_MIXED_EXPECTED


class TestCasefold:
    """Unicode casefold for case-insensitive matching (more aggressive than lower())."""

    def test_upper_to_lower(self):
        """All-caps input folds to lowercase."""
        result = casefold(CASEFOLD_UPPER_INPUT)
        report("casefold", {"input": CASEFOLD_UPPER_INPUT, "output": result})
        assert result == CASEFOLD_UPPER_EXPECTED

    def test_german_eszett(self):
        """German ß expands to 'ss' under casefold (stricter than lower())."""
        result = casefold(CASEFOLD_ESZETT_INPUT)
        report("casefold", {"input": CASEFOLD_ESZETT_INPUT, "output": result})
        assert result == CASEFOLD_ESZETT_EXPECTED

    def test_empty_string(self):
        """[Negative] Empty string returns empty without error."""
        assert casefold(EMPTY_STRING) == ""

    def test_already_lowercase(self):
        """[Edge] Already-lowercase text is returned unchanged (idempotent)."""
        result = casefold(CASEFOLD_ALREADY_LOWER_INPUT)
        assert result == CASEFOLD_ALREADY_LOWER_EXPECTED


class TestToAscii:
    """Transliterate non-ASCII characters to closest ASCII via unidecode (lossy)."""

    def test_accented_characters(self):
        """French accent: 'café' → 'cafe' (é drops accent)."""
        result = to_ascii(ASCII_ACCENT_INPUT)
        report("to_ascii", {"input": ASCII_ACCENT_INPUT, "output": result})
        assert result == ASCII_ACCENT_EXPECTED

    def test_umlaut(self):
        """German umlaut: 'München' → 'Munchen' (ü drops diaeresis)."""
        result = to_ascii(ASCII_UMLAUT_INPUT)
        report("to_ascii", {"input": ASCII_UMLAUT_INPUT, "output": result})
        assert result == ASCII_UMLAUT_EXPECTED

    def test_empty_string(self):
        """[Negative] Empty string returns empty without error."""
        assert to_ascii(EMPTY_STRING) == ""

    def test_cjk_transliteration(self):
        """[Edge] CJK characters (東京都) transliterate to pinyin romanization."""
        result = to_ascii(CJK_INPUT)
        report("to_ascii [CJK]", {"input": CJK_INPUT, "output": result})
        assert result == CJK_ASCII_EXPECTED

    def test_arabic_does_not_crash(self):
        """[Edge] Arabic script (القاهرة) does not raise — returns some string."""
        result = to_ascii(ARABIC_INPUT)
        report("to_ascii [Arabic]", {"input": ARABIC_INPUT, "output": result})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_emoji_stripped(self):
        """[Edge] Emoji in address text is stripped; surrounding ASCII preserved."""
        result = to_ascii(EMOJI_INPUT)
        report("to_ascii [emoji]", {"input": EMOJI_INPUT, "output": result})
        assert "123" in result
        assert "Main" in result


class TestNormalizeForMatching:
    """End-to-end normalization chain: unicode → punctuation → whitespace → casefold."""

    def test_full_pipeline(self):
        """Accented + mixed-case + extra whitespace address normalizes correctly."""
        result = normalize_for_matching(MATCH_UNICODE_INPUT)
        report("normalize_for_matching", {"input": MATCH_UNICODE_INPUT, "output": result})
        assert result == MATCH_UNICODE_EXPECTED

    def test_fullwidth_and_unicode(self):
        """Fullwidth comma in 'TOKYO，JAPAN' converts and casefolds in one pass."""
        result = normalize_for_matching(MATCH_FULLWIDTH_INPUT)
        report("normalize_for_matching", {"input": MATCH_FULLWIDTH_INPUT, "output": result})
        assert result == MATCH_FULLWIDTH_EXPECTED

    def test_empty_string(self):
        """[Negative] Empty string goes through full chain and returns empty."""
        result = normalize_for_matching(EMPTY_STRING)
        report("normalize_for_matching [negative]", {"input": repr(EMPTY_STRING), "output": result})
        assert result == ""

    def test_whitespace_only(self):
        """[Negative] Whitespace-only string normalizes to empty after collapse + strip."""
        result = normalize_for_matching(WHITESPACE_ONLY)
        report("normalize_for_matching [negative]", {"input": repr(WHITESPACE_ONLY), "output": result})
        assert result == ""


# ── Tokenizer & N-grams ─────────────────────────────────────────────────────


class TestTokenize:
    """Whitespace tokenizer — splits text into word tokens."""

    def test_basic(self):
        """Two words separated by space produce a two-element list."""
        result = tokenize(TOKENIZE_BASIC_INPUT)
        report("tokenize", {"input": TOKENIZE_BASIC_INPUT, "output": result})
        assert result == TOKENIZE_BASIC_EXPECTED

    def test_empty(self):
        """[Negative] Empty string produces empty list."""
        result = tokenize(TOKENIZE_EMPTY_INPUT)
        report("tokenize", {"input": TOKENIZE_EMPTY_INPUT, "output": result})
        assert result == TOKENIZE_EMPTY_EXPECTED

    def test_filters_empty_strings(self):
        """Double space between words does not produce empty-string tokens."""
        result = tokenize(TOKENIZE_DOUBLE_SPACE_INPUT)
        report("tokenize", {"input": TOKENIZE_DOUBLE_SPACE_INPUT, "output": result})
        assert result == TOKENIZE_DOUBLE_SPACE_EXPECTED

    def test_whitespace_only(self):
        """[Negative] Tabs/newlines/spaces only → empty list, no ghost tokens."""
        result = tokenize(WHITESPACE_ONLY)
        report("tokenize [negative]", {"input": repr(WHITESPACE_ONLY), "output": result})
        assert result == []

    def test_single_token(self):
        """[Edge] Single word with no spaces produces a one-element list."""
        result = tokenize(TOKENIZE_SINGLE_INPUT)
        report("tokenize [edge]", {"input": TOKENIZE_SINGLE_INPUT, "output": result})
        assert result == TOKENIZE_SINGLE_EXPECTED


class TestExtractNgrams:
    """Generate contiguous n-grams (unigrams, bigrams, …) from a token list."""

    def test_unigrams_and_bigrams(self):
        """3 tokens with max_n=2 produce all unigrams and adjacent bigrams, no trigrams."""
        ngrams = extract_ngrams(NGRAMS_TOKENS, min_n=1, max_n=2)
        report("extract_ngrams", {"tokens": NGRAMS_TOKENS, "max_n": 2, "ngrams": ngrams})
        for expected in NGRAMS_MAX_2_MUST_INCLUDE:
            assert expected in ngrams, f"Missing ngram: {expected}"
        for excluded in NGRAMS_MAX_2_MUST_EXCLUDE:
            assert excluded not in ngrams, f"Should not contain: {excluded}"

    def test_max_n_larger_than_tokens(self):
        """max_n=4 with only 2 tokens caps at bigram; no IndexError."""
        ngrams = extract_ngrams(NGRAMS_TWO_TOKENS, min_n=1, max_n=4)
        report("extract_ngrams", {"tokens": NGRAMS_TWO_TOKENS, "max_n": 4, "ngrams": ngrams})
        assert NGRAMS_TWO_EXPECTED_BIGRAM in ngrams
        assert len([ng for ng in ngrams if " " in ng]) == 1

    def test_empty_tokens(self):
        """[Negative] Empty token list returns empty ngram list."""
        result = extract_ngrams([], min_n=1, max_n=4)
        report("extract_ngrams", {"tokens": [], "output": result})
        assert result == []

    def test_single_token_no_bigrams(self):
        """[Edge] Single token produces only a unigram — no bigrams possible."""
        ngrams = extract_ngrams(NGRAMS_SINGLE_TOKEN, min_n=1, max_n=2)
        report("extract_ngrams [edge]", {"tokens": NGRAMS_SINGLE_TOKEN, "ngrams": ngrams})
        assert ngrams == NGRAMS_SINGLE_EXPECTED
        assert not any(" " in ng for ng in ngrams), "Single token should produce no bigrams"


# ── Build raw address ───────────────────────────────────────────────────────


class TestBuildRawAddress:
    """Concatenate address_1/2/3 lines into a single raw address string."""

    def test_concatenates_non_empty(self):
        """Three populated lines join with single space separator."""
        result = build_raw_address(RAW_LINES_FULL)
        report("build_raw_address", {"input_lines": RAW_LINES_FULL, "output": result})
        assert result == RAW_LINES_FULL_EXPECTED

    def test_skips_empty_and_none(self):
        """Empty strings and None values in the list are silently skipped."""
        result = build_raw_address(RAW_LINES_GAPS)
        report("build_raw_address", {"input_lines": RAW_LINES_GAPS, "output": result})
        assert result == RAW_LINES_GAPS_EXPECTED

    def test_all_empty(self):
        """[Negative] All three lines empty → returns empty string."""
        result = build_raw_address(RAW_LINES_EMPTY)
        report("build_raw_address", {"input_lines": RAW_LINES_EMPTY, "output": result})
        assert result == RAW_LINES_EMPTY_EXPECTED

    def test_strips_whitespace(self):
        """Leading/trailing whitespace on each line is stripped before joining."""
        result = build_raw_address(RAW_LINES_PADDED)
        report("build_raw_address", {"input_lines": RAW_LINES_PADDED, "output": result})
        assert result == RAW_LINES_PADDED_EXPECTED

    def test_whitespace_only_lines(self):
        """[Negative] Lines containing only spaces/tabs/newlines treated as empty."""
        result = build_raw_address(RAW_LINES_WHITESPACE_ONLY)
        report("build_raw_address [negative]", {"input_lines": RAW_LINES_WHITESPACE_ONLY, "output": result})
        assert result == RAW_LINES_WHITESPACE_ONLY_EXPECTED

    def test_empty_list(self):
        """[Negative] No lines at all (empty list) returns empty string."""
        result = build_raw_address(RAW_LINES_EMPTY_LIST)
        report("build_raw_address [negative]", {"input_lines": RAW_LINES_EMPTY_LIST, "output": result})
        assert result == RAW_LINES_EMPTY_LIST_EXPECTED


# ── Redact PII ──────────────────────────────────────────────────────────────


class TestRedactPii:
    """Redact address text for safe logging — keep first N chars, mask the rest."""

    def test_long_string(self):
        """String longer than keep_chars shows first 5 chars + '[redacted]' suffix."""
        result = redact_pii(REDACT_LONG_INPUT)
        assert result == REDACT_LONG_EXPECTED

    def test_short_string(self):
        """String shorter than keep_chars shows full text + '…' suffix (no redaction)."""
        result = redact_pii(REDACT_SHORT_INPUT)
        assert result == REDACT_SHORT_EXPECTED

    def test_empty(self):
        """[Negative] Empty string returns '<empty>' sentinel."""
        assert redact_pii("") == REDACT_EMPTY_EXPECTED

    def test_none(self):
        """[Negative] None input returns '<empty>' sentinel without raising."""
        assert redact_pii(None) == REDACT_EMPTY_EXPECTED

    def test_boundary_exactly_keep_chars(self):
        """[Edge] String exactly equal to keep_chars (5) takes short path — no '[redacted]'."""
        result = redact_pii(REDACT_BOUNDARY_INPUT)
        report("redact_pii [boundary]", {"input": REDACT_BOUNDARY_INPUT, "output": result})
        assert result == REDACT_BOUNDARY_EXPECTED

    def test_whitespace_only(self):
        """[Negative] Whitespace-only string treated as empty → '<empty>'."""
        result = redact_pii(REDACT_WHITESPACE_INPUT)
        report("redact_pii [negative]", {"input": repr(REDACT_WHITESPACE_INPUT), "output": result})
        assert result == REDACT_WHITESPACE_EXPECTED


# ── Preprocess (state-based) ────────────────────────────────────────────────


class TestPreprocess:
    """Full Step 0 entry point — reads address_1/2/3 from state, writes raw_address + normalized."""

    def test_builds_raw_address(self):
        """Three populated address lines produce correct raw_address and normalized form."""
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
        """None and empty-string lines are silently skipped; only address_1 appears."""
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
        """[Negative] All three lines None → raw_address is empty + 'no_address_data' warning."""
        state = {"address_1": None, "address_2": None, "address_3": None}
        report("preprocess input", state)
        result = preprocess(state)
        report("preprocess output", {"raw_address": result["raw_address"], "warnings": result.get("warnings", [])})
        assert result["raw_address"] == ""
        assert result["normalized"] == ""
        assert WARN_NO_ADDRESS_DATA in result.get("warnings", [])

    def test_preserves_existing_state(self):
        """Extra keys in the state dict (e.g. country_code) are preserved, not overwritten."""
        state = {
            "address_1": SAMPLE_ADDRESS_1,
            "address_2": None,
            "address_3": None,
            "country_code": SAMPLE_COUNTRY_CODE,
        }
        result = preprocess(state)
        report("preprocess output", {"raw_address": result["raw_address"], "country_code": result["country_code"]})
        assert result["country_code"] == SAMPLE_COUNTRY_CODE

    def test_whitespace_only_lines_warn(self):
        """Address lines that are only whitespace should be treated as empty."""
        state = dict(PREPROCESS_WHITESPACE_STATE)
        report("preprocess [negative]", state)
        result = preprocess(state)
        report("preprocess output", {"raw_address": result["raw_address"], "warnings": result.get("warnings", [])})
        assert result["raw_address"] == ""
        assert result["normalized"] == ""
        assert WARN_NO_ADDRESS_DATA in result.get("warnings", [])

    def test_missing_keys_entirely(self):
        """State dict with no address keys at all — should not crash."""
        state = dict(PREPROCESS_MISSING_KEYS_STATE)
        report("preprocess [negative]", state)
        result = preprocess(state)
        report("preprocess output", {"raw_address": result["raw_address"], "warnings": result.get("warnings", [])})
        assert result["raw_address"] == ""
        assert result["normalized"] == ""
        assert WARN_NO_ADDRESS_DATA in result.get("warnings", [])
