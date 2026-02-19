"""
Persistence service (Step 8).

Assembles the final result dict from session state.
For local/dev: only formats the result. For production (deferred):
writes to Cloud SQL + GCS.

Operates on session state dict:
  Reads:  All state keys accumulated by previous steps.
  Writes: state["final_result"]
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def persist(state: dict) -> dict:
    """Step 8: assemble the final result from state.

    Returns:
        The mutated state dict with state["final_result"] populated.
    """
    status = state.get("status", "rejected")

    # Map internal status to output status
    if status == "resolved":
        output_status = "validated"
    elif status == "validated":
        output_status = "validated"
    elif status == "needs_review":
        output_status = "needs_review"
    else:
        output_status = "rejected"

    final_result = {
        # Original input
        "address_1": state.get("address_1"),
        "address_2": state.get("address_2"),
        "address_3": state.get("address_3"),
        "country_code": state.get("country_code"),
        # Extracted fields
        "town": state.get("town_candidate"),
        "street": state.get("libpostal_street"),
        "building": state.get("libpostal_building"),
        "postal_code": state.get("libpostal_postal_code"),
        # Pipeline metadata
        "status": output_status,
        "confidence_score": round(state.get("confidence", 0.0), 4),
        "parser_source": state.get("parser_source"),
        "geonames_match": state.get("exact_match", False) or state.get("scan_match", False),
        "geonames_id": state.get("geonames_id"),
        "normalized_town": state.get("town_candidate"),
        "warnings": "; ".join(state.get("warnings", [])),
        "review_reason": _compute_review_reason(state, output_status),
        # Mismatch info
        "mismatch_detected": state.get("mismatch_detected", False),
        "suggested_country_code": state.get("suggested_country_code"),
    }

    state["final_result"] = final_result
    logger.debug(
        "Persist: status=%s, town=%s, confidence=%.2f",
        output_status,
        final_result.get("town"),
        final_result.get("confidence_score", 0.0),
    )
    return state


def _compute_review_reason(state: dict, status: str) -> str | None:
    """Determine the review reason based on pipeline state."""
    if status == "validated":
        return None

    if not state.get("raw_address"):
        return "no_address_data"

    if state.get("status") == "needs_review":
        if state.get("llm_result"):
            return "geonames_no_match"
        return "no_town_candidate"

    return "no_town_candidate"
