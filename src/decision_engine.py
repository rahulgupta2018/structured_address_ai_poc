"""
Decision engine (Step 5).

Assigns final status, confidence, parser_source, and review_reason
based on the outputs of each pipeline stage.

Key invariant: No row is ever `validated` without a confirmed GeoNames match.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import config
from .schemas import (
    AddressInput,
    AddressOutput,
    GeoNamesMatch,
    LLMResponse,
    ParserSource,
    Status,
)
from .parser_libpostal import ParseResult
from .preprocess import redact_pii

logger = logging.getLogger(__name__)


def decide(
    inp: AddressInput,
    output: AddressOutput,
    libpostal_result: Optional[ParseResult],
    libpostal_match: Optional[GeoNamesMatch],
    scan_match: Optional[GeoNamesMatch],
    llm_response: Optional[LLMResponse],
    llm_match: Optional[GeoNamesMatch],
) -> AddressOutput:
    """
    Walk the waterfall and assign final status.

    Priority order:
    1. libpostal → GeoNames exact match  (source=libpostal)
    2. GeoNames raw-address scan         (source=geonames_scan)
    3. LLM → GeoNames re-validation      (source=llm)
    4. LLM proposed but unverified        → needs_review
    5. No candidate at all                → rejected

    Args:
        inp:              Original address input.
        output:           Partially populated AddressOutput (from_input).
        libpostal_result: ParseResult from libpostal (may be None).
        libpostal_match:  GeoNames match against libpostal's town candidate.
        scan_match:       GeoNames match from raw-address scan.
        llm_response:     LLM fallback response (may be None).
        llm_match:        GeoNames match against LLM's town candidate.

    Returns:
        Fully decided AddressOutput with status, confidence, etc.
    """
    # ── Populate secondary fields from libpostal ─────────────────────────
    if libpostal_result is not None:
        output.street = output.street or libpostal_result.street
        output.building = output.building or libpostal_result.building
        output.postal_code = output.postal_code or libpostal_result.postal_code
        output.warnings.extend(libpostal_result.warnings)

    # ── Path 1: libpostal → exact GeoNames match ────────────────────────
    if libpostal_match is not None and libpostal_match.matched:
        _apply_validated(
            output,
            town=libpostal_match.matched_name,
            source=ParserSource.LIBPOSTAL,
            match=libpostal_match,
        )
        logger.debug(
            "Row validated via libpostal: town=%s confidence=%.2f",
            redact_pii(output.town),
            output.confidence_score,
        )
        return output

    # ── Path 2: GeoNames raw-address scan match ─────────────────────────
    if scan_match is not None and scan_match.matched:
        _apply_validated(
            output,
            town=scan_match.matched_name,
            source=ParserSource.GEONAMES_SCAN,
            match=scan_match,
        )
        logger.debug(
            "Row validated via scan: town=%s confidence=%.2f",
            redact_pii(output.town),
            output.confidence_score,
        )
        return output

    # ── Path 3: LLM fallback ────────────────────────────────────────────
    if llm_response is not None:
        # 3a: LLM candidate confirmed by GeoNames
        if llm_match is not None and llm_match.matched:
            _apply_validated(
                output,
                town=llm_match.matched_name,
                source=ParserSource.LLM,
                match=llm_match,
            )
            # Adjust confidence: blend LLM confidence with GeoNames match
            output.confidence_score = config.CONFIDENCE_LLM_CONFIRMED
            logger.debug(
                "Row validated via LLM+GeoNames: town=%s confidence=%.2f",
                redact_pii(output.town),
                output.confidence_score,
            )
            return output

        # 3b: LLM proposed a town but GeoNames could not verify it
        if llm_response.town_candidate:
            output.town = llm_response.town_candidate
            output.status = Status.NEEDS_REVIEW
            output.confidence_score = config.CONFIDENCE_LLM_UNVERIFIED
            output.parser_source = ParserSource.LLM
            output.geonames_match = False
            output.review_reason = "geonames_no_match"
            output.warnings.append("geonames_no_match")
            logger.debug(
                "Row needs_review (LLM unverified): town=%s",
                redact_pii(output.town),
            )
            return output

        # 3c: LLM flagged for manual review
        if llm_response.needs_manual_review:
            output.status = Status.NEEDS_REVIEW
            output.confidence_score = config.CONFIDENCE_LLM_UNVERIFIED
            output.parser_source = ParserSource.LLM
            output.review_reason = "llm_flagged_manual_review"
            return output

    # ── Path 4: No candidate from any source ─────────────────────────────
    output.status = Status.REJECTED
    output.confidence_score = config.CONFIDENCE_REJECTED
    if not inp.has_address:
        output.review_reason = "no_address_data"
    else:
        output.review_reason = "no_town_candidate"
    logger.debug("Row rejected: reason=%s", output.review_reason)
    return output


def _apply_validated(
    output: AddressOutput,
    town: Optional[str],
    source: ParserSource,
    match: GeoNamesMatch,
) -> None:
    """Apply a validated GeoNames match to the output row."""
    output.town = town
    output.status = Status.VALIDATED
    output.confidence_score = match.confidence
    output.parser_source = source
    output.geonames_match = True
    output.geonames_id = match.geonames_id
    output.normalized_town = match.matched_name
