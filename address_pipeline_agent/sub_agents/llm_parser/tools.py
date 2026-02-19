"""
LLM tool functions for the address parser agent.

These are plain Python functions from services/geonames_repo.py,
re-exported here so ADK can register them as LLM-callable tools.
ADK auto-generates tool descriptions from function docstrings.
"""

from services.geonames_repo import (
    list_countries_for_city,
    query_city,
    query_city_by_admin1,
    query_city_fuzzy,
    query_postal_code,
)

__all__ = [
    "query_city",
    "list_countries_for_city",
    "query_postal_code",
    "query_city_fuzzy",
    "query_city_by_admin1",
]
