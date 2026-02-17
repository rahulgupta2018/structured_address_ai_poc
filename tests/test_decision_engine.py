"""Tests for the decision engine."""

import pytest

from src import config
from src.decision_engine import decide
from src.parser_libpostal import ParseResult
from src.schemas import (
    AddressInput,
    AddressOutput,
    GeoNamesMatch,
    LLMResponse,
    ParserSource,
    Status,
)


def _make_input(**kwargs) -> AddressInput:
    defaults = {
        "address_1": "Test Street 1",
        "address_2": "Test City",
        "country_code": "DE",
    }
    defaults.update(kwargs)
    return AddressInput(**defaults)


def _make_output(inp: AddressInput) -> AddressOutput:
    return AddressOutput.from_input(inp)


class TestDecideLibpostalPath:
    def test_validated_via_libpostal_exact_match(self):
        inp = _make_input()
        output = _make_output(inp)
        libpostal = ParseResult(town_candidate="Berlin")
        geo_match = GeoNamesMatch(
            matched=True,
            geonames_id=2950159,
            matched_name="Berlin",
            match_type="primary",
            confidence=config.CONFIDENCE_EXACT_PRIMARY,
        )

        result = decide(inp, output, libpostal, geo_match, None, None, None)

        assert result.status == Status.VALIDATED
        assert result.parser_source == ParserSource.LIBPOSTAL
        assert result.town == "Berlin"
        assert result.geonames_match is True
        assert result.confidence_score == config.CONFIDENCE_EXACT_PRIMARY

    def test_secondary_fields_populated(self):
        inp = _make_input()
        output = _make_output(inp)
        libpostal = ParseResult(
            town_candidate="Berlin",
            street="Hauptstraße",
            building="5",
            postal_code="10115",
        )
        geo_match = GeoNamesMatch(
            matched=True, geonames_id=1, matched_name="Berlin",
            match_type="primary", confidence=1.0,
        )

        result = decide(inp, output, libpostal, geo_match, None, None, None)

        assert result.street == "Hauptstraße"
        assert result.building == "5"
        assert result.postal_code == "10115"


class TestDecideScanPath:
    def test_validated_via_scan(self):
        inp = _make_input()
        output = _make_output(inp)
        scan_match = GeoNamesMatch(
            matched=True,
            geonames_id=2867714,
            matched_name="München",
            match_type="fuzzy",
            confidence=config.CONFIDENCE_FUZZY_SCAN,
        )

        result = decide(inp, output, None, None, scan_match, None, None)

        assert result.status == Status.VALIDATED
        assert result.parser_source == ParserSource.GEONAMES_SCAN
        assert result.town == "München"
        assert result.confidence_score == config.CONFIDENCE_FUZZY_SCAN


class TestDecideLLMPath:
    def test_validated_via_llm_confirmed(self):
        inp = _make_input()
        output = _make_output(inp)
        llm_resp = LLMResponse(
            town_candidate="Berlin", confidence=0.9, needs_manual_review=False
        )
        llm_match = GeoNamesMatch(
            matched=True,
            geonames_id=2950159,
            matched_name="Berlin",
            match_type="primary",
            confidence=config.CONFIDENCE_EXACT_PRIMARY,
        )

        result = decide(inp, output, None, None, None, llm_resp, llm_match)

        assert result.status == Status.VALIDATED
        assert result.parser_source == ParserSource.LLM
        assert result.confidence_score == config.CONFIDENCE_LLM_CONFIRMED

    def test_needs_review_llm_unverified(self):
        inp = _make_input()
        output = _make_output(inp)
        llm_resp = LLMResponse(
            town_candidate="SomeVillage", confidence=0.6, needs_manual_review=False
        )
        llm_match = GeoNamesMatch(matched=False)

        result = decide(inp, output, None, None, None, llm_resp, llm_match)

        assert result.status == Status.NEEDS_REVIEW
        assert result.town == "SomeVillage"
        assert result.review_reason == "geonames_no_match"
        assert result.confidence_score == config.CONFIDENCE_LLM_UNVERIFIED

    def test_needs_review_llm_flagged(self):
        inp = _make_input()
        output = _make_output(inp)
        llm_resp = LLMResponse(
            town_candidate=None, confidence=0.0, needs_manual_review=True
        )

        result = decide(inp, output, None, None, None, llm_resp, None)

        assert result.status == Status.NEEDS_REVIEW
        assert result.review_reason == "llm_flagged_manual_review"


class TestDecideRejectedPath:
    def test_rejected_no_candidate(self):
        inp = _make_input()
        output = _make_output(inp)

        result = decide(inp, output, None, None, None, None, None)

        assert result.status == Status.REJECTED
        assert result.confidence_score == config.CONFIDENCE_REJECTED
        assert result.review_reason == "no_town_candidate"

    def test_rejected_no_address(self):
        inp = _make_input(address_1=None, address_2=None)
        output = _make_output(inp)

        result = decide(inp, output, None, None, None, None, None)

        assert result.status == Status.REJECTED
        assert result.review_reason == "no_address_data"


class TestInvariant:
    """No row is ever validated without a GeoNames match."""

    def test_llm_without_geonames_never_validated(self):
        inp = _make_input()
        output = _make_output(inp)
        llm_resp = LLMResponse(
            town_candidate="FakeTown", confidence=0.99, needs_manual_review=False
        )
        # No GeoNames match
        result = decide(inp, output, None, None, None, llm_resp, GeoNamesMatch(matched=False))

        assert result.status != Status.VALIDATED
