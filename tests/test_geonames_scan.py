"""Tests for the GeoNames scan module (raw-address token/fuzzy scan)."""

import pytest

from src.geonames_loader import CityRecord, GeoNamesIndex
from src.geonames_scan import scan_raw_address
from src.preprocess import normalize_for_matching


def _build_scan_index() -> GeoNamesIndex:
    """Build a test GeoNames index with a few cities for scan testing."""
    index = GeoNamesIndex()

    cities = [
        CityRecord(
            geonames_id=2867714,
            name="München",
            ascii_name="Munchen",
            alternate_names=["Munich"],
            country_code="DE",
            admin1_code="02",
            population=1_260_000,
            latitude=48.1351,
            longitude=11.5820,
        ),
        CityRecord(
            geonames_id=2950159,
            name="Berlin",
            ascii_name="Berlin",
            alternate_names=["Берлин"],
            country_code="DE",
            admin1_code="16",
            population=3_645_000,
            latitude=52.5200,
            longitude=13.4050,
        ),
        CityRecord(
            geonames_id=2925533,
            name="Frankfurt am Main",
            ascii_name="Frankfurt am Main",
            alternate_names=["Frankfurt", "Francfort"],
            country_code="DE",
            admin1_code="05",
            population=753_000,
            latitude=50.1109,
            longitude=8.6821,
        ),
        CityRecord(
            geonames_id=2988507,
            name="Paris",
            ascii_name="Paris",
            alternate_names=["Paname"],
            country_code="FR",
            admin1_code="11",
            population=2_138_000,
            latitude=48.8566,
            longitude=2.3522,
        ),
    ]

    for city in cities:
        cc = city.country_code.upper()
        if cc not in index.by_country:
            index.by_country[cc] = {}
            index.country_names[cc] = set()

        for name in [city.name, city.ascii_name] + city.alternate_names:
            norm = name.strip().lower()
            if norm:
                index.country_names[cc].add(norm)
                if norm not in index.by_country[cc]:
                    index.by_country[cc][norm] = []
                index.by_country[cc][norm].append(city)

    return index


@pytest.fixture
def index() -> GeoNamesIndex:
    return _build_scan_index()


class TestScanRawAddress:
    def test_exact_token_match(self, index: GeoNamesIndex):
        raw = normalize_for_matching("Hauptstraße 5, Berlin, 10115")
        result = scan_raw_address(index, raw, "DE")
        assert result is not None
        assert result.matched is True
        assert result.geonames_id == 2950159
        assert result.matched_name is not None
        assert "berlin" in result.matched_name.lower()

    def test_multi_word_match(self, index: GeoNamesIndex):
        raw = normalize_for_matching("Zeil 10, Frankfurt am Main")
        result = scan_raw_address(index, raw, "DE")
        assert result is not None
        assert result.matched is True
        # Should prefer "frankfurt am main" (longer match) over just "frankfurt"
        assert result.geonames_id == 2925533

    def test_no_match_wrong_country(self, index: GeoNamesIndex):
        raw = normalize_for_matching("Hauptstraße 5, Berlin, 10115")
        result = scan_raw_address(index, raw, "FR")
        # Berlin shouldn't be found in FR's index
        assert result.matched is False

    def test_no_match_no_city(self, index: GeoNamesIndex):
        raw = normalize_for_matching("123 Unknown Road, Some Place")
        result = scan_raw_address(index, raw, "DE")
        assert result.matched is False

    def test_empty_address(self, index: GeoNamesIndex):
        result = scan_raw_address(index, "", "DE")
        assert result.matched is False

    def test_unknown_country(self, index: GeoNamesIndex):
        raw = normalize_for_matching("Berlin")
        result = scan_raw_address(index, raw, "ZZ")
        assert result.matched is False
