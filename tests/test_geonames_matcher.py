"""Tests for the GeoNames matcher (exact match validation)."""

import pytest

from src.geonames_loader import CityRecord, GeoNamesIndex
from src.geonames_matcher import match_exact
from src.preprocess import normalize_for_matching
from src import config


def _build_test_index() -> GeoNamesIndex:
    """Build a small test GeoNames index."""
    index = GeoNamesIndex()

    # Munich, Germany
    munich = CityRecord(
        geonames_id=2867714,
        name="München",
        ascii_name="Munchen",
        alternate_names=["Munich", "Мюнхен", "ミュンヘン"],
        country_code="DE",
        admin1_code="02",
        population=1_260_000,
        latitude=48.1351,
        longitude=11.5820,
    )

    # Paris, France
    paris_fr = CityRecord(
        geonames_id=2988507,
        name="Paris",
        ascii_name="Paris",
        alternate_names=["Paname", "Парижь"],
        country_code="FR",
        admin1_code="11",
        population=2_138_000,
        latitude=48.8566,
        longitude=2.3522,
    )

    # Paris, Texas, US (same name, different country)
    paris_tx = CityRecord(
        geonames_id=4717560,
        name="Paris",
        ascii_name="Paris",
        alternate_names=[],
        country_code="US",
        admin1_code="TX",
        population=25_000,
        latitude=33.6609,
        longitude=-95.5555,
    )

    # Register cities
    for city in [munich, paris_fr, paris_tx]:
        cc = city.country_code.upper()
        if cc not in index.by_country:
            index.by_country[cc] = {}
            index.country_names[cc] = set()

        for name in [city.name, city.ascii_name] + city.alternate_names:
            norm = normalize_for_matching(name)
            if norm:
                index.country_names[cc].add(norm)
                if norm not in index.by_country[cc]:
                    index.by_country[cc][norm] = []
                index.by_country[cc][norm].append(city)

    return index


@pytest.fixture
def index() -> GeoNamesIndex:
    return _build_test_index()


class TestMatchExact:
    def test_primary_name_match(self, index: GeoNamesIndex):
        result = match_exact(index, "München", "DE")
        assert result.matched is True
        assert result.geonames_id == 2867714
        assert result.match_type == "primary"
        assert result.confidence == config.CONFIDENCE_EXACT_PRIMARY

    def test_ascii_name_match(self, index: GeoNamesIndex):
        result = match_exact(index, "Munchen", "DE")
        assert result.matched is True
        assert result.geonames_id == 2867714

    def test_alternate_name_match(self, index: GeoNamesIndex):
        result = match_exact(index, "Munich", "DE")
        assert result.matched is True
        assert result.geonames_id == 2867714
        assert result.match_type == "alternate"
        assert result.confidence == config.CONFIDENCE_EXACT_ALTERNATE

    def test_case_insensitive(self, index: GeoNamesIndex):
        result = match_exact(index, "PARIS", "FR")
        assert result.matched is True
        assert result.geonames_id == 2988507

    def test_country_scoping(self, index: GeoNamesIndex):
        """Paris in FR should return the French city, not the US one."""
        result_fr = match_exact(index, "Paris", "FR")
        result_us = match_exact(index, "Paris", "US")

        assert result_fr.matched is True
        assert result_fr.geonames_id == 2988507

        assert result_us.matched is True
        assert result_us.geonames_id == 4717560

    def test_no_match(self, index: GeoNamesIndex):
        result = match_exact(index, "Springfield", "US")
        assert result.matched is False
        assert result.geonames_id is None

    def test_wrong_country(self, index: GeoNamesIndex):
        """München should NOT match in FR."""
        result = match_exact(index, "München", "FR")
        assert result.matched is False

    def test_empty_candidate(self, index: GeoNamesIndex):
        result = match_exact(index, "", "DE")
        assert result.matched is False

    def test_unknown_country(self, index: GeoNamesIndex):
        result = match_exact(index, "Paris", "ZZ")
        assert result.matched is False
