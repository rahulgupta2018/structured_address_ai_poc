"""Tests for geonames_repo service — SQLite data access layer."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from services.geonames_repo import (
    _rows_to_dicts,
    close_connection,
    get_all_normalized_names,
    get_connection,
    list_countries_for_city,
    query_city,
    query_city_by_admin1,
    query_city_fuzzy,
    query_postal_code,
    reset_connection,
    resolve_all_cities_by_name,
    resolve_city_by_name,
    search_postal_by_place_name,
)

from tests.test_services.report import report


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SAMPLE DATA — edit these to test with your own cities/codes            ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Primary city for query/lookup tests
SAMPLE_CITY_NAME = "London"
SAMPLE_CITY_COUNTRY = "GB"
SAMPLE_CITY_GEONAMEID = 2643743

# Alternate name test
SAMPLE_ALT_NAME = "NYC"
SAMPLE_ALT_COUNTRY = "US"
SAMPLE_ALT_GEONAMEID = 5128581

# Ambiguous city (multiple matches)
SAMPLE_AMBIGUOUS_CITY = "Springfield"
SAMPLE_AMBIGUOUS_COUNTRY = "US"
SAMPLE_AMBIGUOUS_MO_ID = 4409896
SAMPLE_AMBIGUOUS_IL_ID = 4250542

# Postal code lookup
SAMPLE_POSTAL_CODE = "62701"
SAMPLE_POSTAL_COUNTRY = "US"
SAMPLE_POSTAL_PLACE = "Springfield"
SAMPLE_POSTAL_ADMIN1 = "IL"

# Admin1 query
SAMPLE_ADMIN1_CITY = "Springfield"
SAMPLE_ADMIN1_REGION = "Illinois"
SAMPLE_ADMIN1_COUNTRY = "US"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _create_test_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with the GeoNames schema and sample data."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE geonames_cities (
            geonameid INTEGER PRIMARY KEY,
            name TEXT,
            ascii_name TEXT,
            country_code TEXT,
            admin1_code TEXT,
            population INTEGER
        );

        CREATE TABLE geonames_city_names (
            geonameid INTEGER,
            country_code TEXT,
            normalized_name TEXT,
            name_type TEXT
        );

        CREATE TABLE geonames_postal_codes (
            postal_code TEXT,
            place_name TEXT,
            admin_name1 TEXT,
            admin_code1 TEXT,
            country_code TEXT,
            latitude REAL,
            longitude REAL
        );

        CREATE TABLE geonames_admin1 (
            code TEXT,
            name TEXT
        );

        -- Sample cities
        INSERT INTO geonames_cities VALUES (2643743, 'London', 'London', 'GB', 'ENG', 7556900);
        INSERT INTO geonames_cities VALUES (5128581, 'New York City', 'New York City', 'US', 'NY', 8175133);
        INSERT INTO geonames_cities VALUES (4409896, 'Springfield', 'Springfield', 'US', 'MO', 170188);
        INSERT INTO geonames_cities VALUES (4250542, 'Springfield', 'Springfield', 'US', 'IL', 114394);

        -- City names index
        INSERT INTO geonames_city_names VALUES (2643743, 'GB', 'london', 'primary');
        INSERT INTO geonames_city_names VALUES (5128581, 'US', 'new york city', 'primary');
        INSERT INTO geonames_city_names VALUES (5128581, 'US', 'nyc', 'alternate');
        INSERT INTO geonames_city_names VALUES (4409896, 'US', 'springfield', 'primary');
        INSERT INTO geonames_city_names VALUES (4250542, 'US', 'springfield', 'primary');

        -- Postal codes
        INSERT INTO geonames_postal_codes VALUES ('EC1A', 'London', 'Greater London', 'ENG', 'GB', 51.5, -0.1);
        INSERT INTO geonames_postal_codes VALUES ('62701', 'Springfield', 'Illinois', 'IL', 'US', 39.8, -89.6);

        -- Admin1
        INSERT INTO geonames_admin1 VALUES ('US.IL', 'Illinois');
        INSERT INTO geonames_admin1 VALUES ('US.MO', 'Missouri');
        INSERT INTO geonames_admin1 VALUES ('GB.ENG', 'England');
    """)
    return conn


# ── Tests ────────────────────────────────────────────────────────────────────


class TestRowsToDicts:
    def test_converts_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (a TEXT, b INTEGER)")
        conn.execute("INSERT INTO t VALUES ('x', 1)")
        cursor = conn.execute("SELECT * FROM t")
        result = _rows_to_dicts(cursor)
        assert result == [{"a": "x", "b": 1}]

    def test_empty_result(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (a TEXT)")
        cursor = conn.execute("SELECT * FROM t")
        result = _rows_to_dicts(cursor)
        assert result == []


class TestQueryCity:
    @patch("services.geonames_repo.get_connection")
    def test_returns_matching_cities(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = query_city(SAMPLE_CITY_NAME, SAMPLE_CITY_COUNTRY)
        report("query_city", {"input": f"{SAMPLE_CITY_NAME}, {SAMPLE_CITY_COUNTRY}", "rows": len(results), "first": results[0] if results else None})
        assert len(results) == 1
        assert results[0]["name"] == SAMPLE_CITY_NAME
        assert results[0]["geonameid"] == SAMPLE_CITY_GEONAMEID

    @patch("services.geonames_repo.get_connection")
    def test_case_insensitive(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = query_city(SAMPLE_CITY_NAME.upper(), SAMPLE_CITY_COUNTRY.lower())
        assert len(results) == 1

    @patch("services.geonames_repo.get_connection")
    def test_no_match(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = query_city("Xyzzyville", SAMPLE_ALT_COUNTRY)
        assert results == []

    @patch("services.geonames_repo.get_connection")
    def test_alternate_name_adds_matched_field(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = query_city(SAMPLE_ALT_NAME, SAMPLE_ALT_COUNTRY)
        report("query_city (alternate)", {"input": f"{SAMPLE_ALT_NAME}, {SAMPLE_ALT_COUNTRY}", "matched_alternate": results[0].get("matched_alternate_name") if results else None})
        assert len(results) == 1
        assert results[0]["matched_alternate_name"] == SAMPLE_ALT_NAME


class TestListCountriesForCity:
    @patch("services.geonames_repo.get_connection")
    def test_finds_city_across_countries(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = list_countries_for_city(SAMPLE_CITY_NAME.lower())
        countries = {r["country_code"] for r in results}
        report("list_countries_for_city", {"input": SAMPLE_CITY_NAME, "countries_found": countries})
        assert len(results) >= 1
        assert SAMPLE_CITY_COUNTRY in countries

    @patch("services.geonames_repo.get_connection")
    def test_no_results(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = list_countries_for_city("Nonexistent")
        assert results == []


class TestQueryPostalCode:
    @patch("services.geonames_repo.get_connection")
    def test_finds_postal_code(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = query_postal_code(SAMPLE_POSTAL_CODE, SAMPLE_POSTAL_COUNTRY)
        report("query_postal_code", {"input": f"{SAMPLE_POSTAL_CODE}, {SAMPLE_POSTAL_COUNTRY}", "place": results[0]["place_name"] if results else None, "admin1": results[0]["admin_code1"] if results else None})
        assert len(results) == 1
        assert results[0]["place_name"] == SAMPLE_POSTAL_PLACE
        assert results[0]["admin_code1"] == SAMPLE_POSTAL_ADMIN1

    @patch("services.geonames_repo.get_connection")
    def test_no_match(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = query_postal_code("99999", SAMPLE_POSTAL_COUNTRY)
        assert results == []


class TestSearchPostalByPlaceName:
    @patch("services.geonames_repo.get_connection")
    def test_exact_match(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = search_postal_by_place_name(SAMPLE_CITY_NAME)
        assert len(results) >= 1
        assert results[0]["place_name"] == SAMPLE_CITY_NAME

    @patch("services.geonames_repo.get_connection")
    def test_empty_input(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = search_postal_by_place_name("")
        assert results == []


class TestGetAllNormalizedNames:
    @patch("services.geonames_repo.get_connection")
    def test_returns_names_set(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        names = get_all_normalized_names(SAMPLE_ALT_COUNTRY)
        report("get_all_normalized_names", {"country": SAMPLE_ALT_COUNTRY, "names": names})
        assert isinstance(names, set)
        assert SAMPLE_AMBIGUOUS_CITY.lower() in names
        assert "new york city" in names
        assert SAMPLE_ALT_NAME.lower() in names

    @patch("services.geonames_repo.get_connection")
    def test_empty_for_unknown_country(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        names = get_all_normalized_names("ZZ")
        assert names == set()


class TestResolveCityByName:
    @patch("services.geonames_repo.get_connection")
    def test_resolves_to_highest_population(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        result = resolve_city_by_name(SAMPLE_AMBIGUOUS_COUNTRY, SAMPLE_AMBIGUOUS_CITY.lower())
        report("resolve_city_by_name", {"input": f"{SAMPLE_AMBIGUOUS_CITY}, {SAMPLE_AMBIGUOUS_COUNTRY}", "winner": f"{result['name']}(pop={result['population']})" if result else None})
        assert result is not None
        # MO has higher population than IL
        assert result["geonameid"] == SAMPLE_AMBIGUOUS_MO_ID
        assert result["population"] == 170188

    @patch("services.geonames_repo.get_connection")
    def test_no_match_returns_none(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        result = resolve_city_by_name(SAMPLE_AMBIGUOUS_COUNTRY, "xyzzyville")
        assert result is None


class TestResolveAllCitiesByName:
    @patch("services.geonames_repo.get_connection")
    def test_returns_all_matches(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = resolve_all_cities_by_name(SAMPLE_AMBIGUOUS_COUNTRY, SAMPLE_AMBIGUOUS_CITY.lower())
        report("resolve_all_cities_by_name", {"input": SAMPLE_AMBIGUOUS_CITY, "matches": [f"{r['name']}({r['admin1_code']}, pop={r['population']})" for r in results]})
        assert len(results) == 2
        # Sorted by population desc
        assert results[0]["geonameid"] == SAMPLE_AMBIGUOUS_MO_ID  # MO
        assert results[1]["geonameid"] == SAMPLE_AMBIGUOUS_IL_ID  # IL


class TestQueryCityByAdmin1:
    @patch("services.geonames_repo.get_connection")
    def test_finds_city_in_admin1(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = query_city_by_admin1(SAMPLE_ADMIN1_CITY, SAMPLE_ADMIN1_REGION, SAMPLE_ADMIN1_COUNTRY)
        report("query_city_by_admin1", {"input": f"{SAMPLE_ADMIN1_CITY}, {SAMPLE_ADMIN1_REGION}, {SAMPLE_ADMIN1_COUNTRY}", "geonameid": results[0]["geonameid"] if results else None})
        assert len(results) == 1
        assert results[0]["geonameid"] == SAMPLE_AMBIGUOUS_IL_ID
        assert results[0]["admin1_name"] == SAMPLE_ADMIN1_REGION

    @patch("services.geonames_repo.get_connection")
    def test_no_admin1_match(self, mock_conn):
        db = _create_test_db()
        mock_conn.return_value = db

        results = query_city_by_admin1(SAMPLE_ADMIN1_CITY, "NonExistentState", SAMPLE_ADMIN1_COUNTRY)
        assert results == []


class TestConnectionManagement:
    def test_reset_connection(self):
        """reset_connection should close and allow re-initialization."""
        reset_connection()
        # After reset, _connection should be None
        import services.geonames_repo as repo
        assert repo._connection is None
