"""Tests for the preprocess module."""

from src.preprocess import (
    build_raw_address,
    collapse_whitespace,
    extract_ngrams,
    normalize_for_matching,
    normalize_punctuation,
    normalize_unicode,
    to_ascii,
    tokenize,
)


class TestNormalizeUnicode:
    def test_nfkc(self):
        # Full-width 'Ａ' → 'A'
        assert normalize_unicode("Ａ") == "A"

    def test_plain_ascii(self):
        assert normalize_unicode("hello") == "hello"


class TestCollapseWhitespace:
    def test_multiple_spaces(self):
        assert collapse_whitespace("a   b") == "a b"

    def test_tabs_and_newlines(self):
        assert collapse_whitespace("a\t\nb") == "a b"

    def test_leading_trailing(self):
        assert collapse_whitespace("  a  ") == "a"


class TestNormalizePunctuation:
    def test_fullwidth_comma(self):
        assert normalize_punctuation("東京，日本") == "東京,日本"

    def test_fullwidth_period(self):
        assert normalize_punctuation("Test．") == "Test."


class TestToAscii:
    def test_german_umlauts(self):
        assert to_ascii("München") == "Munchen"

    def test_french_accents(self):
        assert to_ascii("Côte d'Ivoire") == "Cote d'Ivoire"

    def test_plain_ascii(self):
        assert to_ascii("London") == "London"


class TestNormalizeForMatching:
    def test_casefold(self):
        result = normalize_for_matching("MÜNCHEN")
        assert result == "münchen"

    def test_whitespace_and_case(self):
        result = normalize_for_matching("  New   York  ")
        assert result == "new york"


class TestBuildRawAddress:
    def test_all_lines(self):
        result = build_raw_address(["line1", "line2", "line3"])
        assert result == "line1 line2 line3"

    def test_nulls_skipped(self):
        result = build_raw_address(["line1", None, "line3"])
        assert result == "line1 line3"

    def test_empty_strings_skipped(self):
        result = build_raw_address(["line1", "", "line3"])
        assert result == "line1 line3"

    def test_all_empty(self):
        result = build_raw_address([None, None, None])
        assert result == ""


class TestTokenize:
    def test_basic(self):
        assert tokenize("hello world") == ["hello", "world"]

    def test_empty(self):
        assert tokenize("") == []


class TestExtractNgrams:
    def test_unigrams_and_bigrams(self):
        tokens = ["a", "b", "c"]
        ngrams = extract_ngrams(tokens, max_n=2)
        assert "a" in ngrams
        assert "a b" in ngrams
        assert "b c" in ngrams
        assert "a b c" not in ngrams

    def test_max_n_4(self):
        tokens = ["new", "york", "city", "center", "plaza"]
        ngrams = extract_ngrams(tokens, max_n=4)
        assert "new york city center" in ngrams
        assert "new york city center plaza" not in ngrams

    def test_single_token(self):
        assert extract_ngrams(["hello"]) == ["hello"]
