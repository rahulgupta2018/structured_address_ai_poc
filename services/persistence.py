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

import json
import logging
from pathlib import Path

from services.normalizer import to_ascii

logger = logging.getLogger(__name__)

# ── Country-code → name lookup (built once from countriesV3.1.json) ──────────

_CODE_TO_COUNTRY: dict[str, str] = {}


def _load_code_to_country() -> None:
    ref_path = (
        Path(__file__).resolve().parent.parent
        / "data"
        / "reference"
        / "countriesV3.1.json"
    )
    if not ref_path.exists():
        logger.warning("Country reference file not found: %s", ref_path)
        return
    with open(ref_path, encoding="utf-8") as fh:
        countries = json.load(fh)
    for entry in countries:
        cc = (entry.get("cca2") or "").upper()
        name = (entry.get("name") or {}).get("common")
        if cc and name:
            _CODE_TO_COUNTRY[cc] = name


_load_code_to_country()


def country_code_to_name(code: str | None) -> str | None:
    """Return the common country name for an ISO alpha-2 code, or None."""
    if not code:
        return None
    return _CODE_TO_COUNTRY.get(code.strip().upper())


# ── Address-line builder (regulatory: 2 lines × 70 chars) ───────────────────

MAX_LINE_LEN = 70


def build_address_lines(
    building: str | None,
    street: str | None,
    postal_code: str | None,
) -> tuple[str, str]:
    """Build address_line_1 and address_line_2 from extracted parts.

    Rules (regulatory):
    * Town and country are excluded — they have dedicated fields.
    * Remaining parts (building, street, postal_code) are concatenated.
    * address_line_1 gets up to 70 characters, address_line_2 the overflow.
    * Words are never split mid-word; break happens on whitespace boundaries.

    Returns:
        (address_line_1, address_line_2) — each at most 70 chars.
    """
    parts = [p.strip() for p in (building, street, postal_code) if p and p.strip()]
    if not parts:
        return ("", "")

    combined = ", ".join(parts)

    if len(combined) <= MAX_LINE_LEN:
        return (combined, "")

    # Split on word boundary at or before the 70-char limit
    line1 = _break_at_word_boundary(combined, MAX_LINE_LEN)
    remainder = combined[len(line1):].lstrip(" ,")

    if len(remainder) > MAX_LINE_LEN:
        remainder = _break_at_word_boundary(remainder, MAX_LINE_LEN)

    return (line1, remainder)


def _break_at_word_boundary(text: str, limit: int) -> str:
    """Return the longest prefix of *text* that fits in *limit* chars
    without cutting a word in the middle."""
    if len(text) <= limit:
        return text

    # Find the last space at or before the limit
    cut = text.rfind(" ", 0, limit + 1)
    if cut <= 0:
        # No space found — take the whole limit (one very long token)
        return text[:limit]

    return text[:cut].rstrip(" ,")


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

    # Build regulatory address lines (2 × 70 chars, no town / country)
    addr_line_1, addr_line_2 = build_address_lines(
        state.get("libpostal_building"),
        state.get("libpostal_street"),
        state.get("libpostal_postal_code"),
    )

    final_result = {
        # Original input (preserved for audit)
        "input_address_1": state.get("address_1"),
        "input_address_2": state.get("address_2"),
        "input_address_3": state.get("address_3"),
        "country_code": state.get("country_code"),
        # Regulatory output: 2 address lines × 70 chars (no town/country)
        "address_line_1": addr_line_1,
        "address_line_2": addr_line_2,
        # Extracted fields
        "town": to_ascii(state["town_candidate"]) if state.get("town_candidate") else None,
        "country": country_code_to_name(state.get("country_code")),
        "street": state.get("libpostal_street"),
        "building": state.get("libpostal_building"),
        "postal_code": state.get("libpostal_postal_code"),
        # Pipeline metadata
        "status": output_status,
        "confidence_score": round(state.get("confidence", 0.0), 4),
        "parser_source": state.get("parser_source"),
        "geonames_match": state.get("exact_match", False) or state.get("scan_match", False),
        "geonames_id": state.get("geonames_id"),
        "normalized_town": to_ascii(state["town_candidate"]) if state.get("town_candidate") else None,
        "warnings": "; ".join(state.get("warnings", [])),
        "review_reason": _compute_review_reason(state, output_status),
        # Mismatch info
        "mismatch_detected": state.get("mismatch_detected", False),
        "suggested_country_code": state.get("suggested_country_code"),
        # LLM usage (0 for deterministic-only rows)
        "llm_calls": state.get("llm_calls", 0),
        "llm_prompt_tokens": state.get("llm_prompt_tokens", 0),
        "llm_completion_tokens": state.get("llm_completion_tokens", 0),
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

    if "country_only_address" in state.get("warnings", []):
        return "country_only_address"

    if state.get("status") == "needs_review":
        if state.get("llm_result"):
            return "geonames_no_match"
        return "no_town_candidate"

    return "no_town_candidate"
