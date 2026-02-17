"""
Top-level pipeline orchestrator.

Wires all stages together:
  Step 0  → Preprocess
  Step 1  → libpostal parse
  Step 2  → GeoNames strict validation
  Step 3  → GeoNames raw-address scan
  Step 4  → LLM fallback (batched)
  Step 5  → Final GeoNames re-validation + decision engine
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from .decision_engine import decide
from .geonames_loader import GeoNamesIndex, load_geonames
from .geonames_matcher import match_exact, match_fuzzy
from .geonames_scan import scan_raw_address
from .io_excel import read_input, write_output
from .llm_ollama import call_llm
from .parser_libpostal import ParseResult, parse_address
from .preprocess import build_raw_address
from .schemas import (
    AddressInput,
    AddressOutput,
    GeoNamesMatch,
    LLMResponse,
    Status,
)

logger = logging.getLogger(__name__)


def run(
    input_path: str | Path,
    output_path: Optional[str | Path] = None,
    *,
    skip_llm: bool = False,
) -> list[AddressOutput]:
    """
    Execute the full pipeline.

    Args:
        input_path:  Path to the input Excel file.
        output_path: Path for the output Excel file.
                     Defaults to ``data/output/<input_stem>_output.xlsx``.
        skip_llm:    If True, skip the LLM fallback (useful for
                     deterministic-only testing).

    Returns:
        List of AddressOutput objects (one per input row).
    """
    t0 = time.perf_counter()

    # ── Load GeoNames index ──────────────────────────────────────────────
    logger.info("Loading GeoNames index from %s …", config.GEONAMES_FILE)
    geonames_index = load_geonames(str(config.GEONAMES_FILE))
    logger.info(
        "GeoNames loaded: %d countries",
        len(geonames_index.by_country),
    )

    # ── Read input ───────────────────────────────────────────────────────
    inputs = read_input(input_path)
    if not inputs:
        logger.warning("No valid input rows. Nothing to do.")
        return []

    logger.info("Processing %d rows …", len(inputs))

    # ── Process each row ─────────────────────────────────────────────────
    results: list[AddressOutput] = []
    llm_queue: list[tuple[int, AddressInput, AddressOutput, Optional[ParseResult]]] = []

    for idx, inp in enumerate(inputs):
        output = AddressOutput.from_input(inp)

        # Input validation
        if not inp.has_address:
            output.status = Status.REJECTED
            output.confidence_score = config.CONFIDENCE_REJECTED
            output.review_reason = "no_address_data"
            results.append(output)
            continue

        # Step 0: Preprocess
        raw_address = build_raw_address(
            [inp.address_1, inp.address_2, inp.address_3]
        )

        # Step 1: libpostal parse
        libpostal_result = parse_address(raw_address)

        # Step 2: GeoNames strict validation (if libpostal found a town)
        libpostal_match: Optional[GeoNamesMatch] = None
        if libpostal_result.town_candidate:
            libpostal_match = match_exact(
                geonames_index,
                libpostal_result.town_candidate,
                inp.country_code,
            )
            if libpostal_match.matched:
                output = decide(
                    inp, output,
                    libpostal_result, libpostal_match,
                    None, None, None,
                )
                results.append(output)
                continue

        # Step 3: GeoNames raw-address scan
        scan_match = scan_raw_address(
            geonames_index, raw_address, inp.country_code
        )
        if scan_match.matched:
            output = decide(
                inp, output,
                libpostal_result, libpostal_match,
                scan_match, None, None,
            )
            results.append(output)
            continue

        # Unresolved → queue for LLM fallback
        llm_queue.append((len(results), inp, output, libpostal_result, raw_address))
        results.append(output)  # placeholder — will be updated in-place

    # ── Step 4 & 5: LLM fallback (batched) ───────────────────────────────
    if llm_queue and not skip_llm:
        logger.info(
            "Sending %d unresolved rows to LLM fallback …", len(llm_queue)
        )
        for batch_start in range(0, len(llm_queue), config.LLM_BATCH_SIZE):
            batch = llm_queue[batch_start : batch_start + config.LLM_BATCH_SIZE]

            for result_idx, inp, output, libpostal_result, raw_address in batch:
                llm_response, llm_warnings = call_llm(
                    inp.address_1,
                    inp.address_2,
                    inp.address_3,
                    inp.country_code,
                    output.warnings.copy(),
                )
                output.warnings.extend(llm_warnings)

                # Step 5: Final GeoNames re-validation
                # Try exact first, then fuzzy if the LLM candidate
                # is a partial/abbreviated form of the official name.
                llm_match: Optional[GeoNamesMatch] = None
                if llm_response and llm_response.town_candidate:
                    llm_match = match_exact(
                        geonames_index,
                        llm_response.town_candidate,
                        inp.country_code,
                    )
                    if not llm_match.matched:
                        llm_match = match_fuzzy(
                            geonames_index,
                            llm_response.town_candidate,
                            inp.country_code,
                            raw_address=raw_address,
                        )

                output = decide(
                    inp, output,
                    libpostal_result, None,  # libpostal match already failed
                    None,  # scan already failed
                    llm_response, llm_match,
                )
                results[result_idx] = output
    elif llm_queue and skip_llm:
        logger.info(
            "Skipping LLM fallback for %d unresolved rows (skip_llm=True)",
            len(llm_queue),
        )
        for result_idx, inp, output, libpostal_result, raw_address in llm_queue:
            output = decide(
                inp, output,
                libpostal_result, None,
                None, None, None,
            )
            results[result_idx] = output

    # ── Write output ─────────────────────────────────────────────────────
    if output_path is None:
        stem = Path(input_path).stem
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = config.OUTPUT_DIR / f"{stem}_output_{ts}.xlsx"

    write_output(results, output_path)

    elapsed = time.perf_counter() - t0
    _log_summary(results, elapsed)

    return results


def _log_summary(results: list[AddressOutput], elapsed: float) -> None:
    """Print a summary of pipeline results."""
    total = len(results)
    validated = sum(1 for r in results if r.status == Status.VALIDATED)
    review = sum(1 for r in results if r.status == Status.NEEDS_REVIEW)
    rejected = sum(1 for r in results if r.status == Status.REJECTED)

    logger.info(
        "Pipeline complete: %d rows in %.1fs — "
        "validated=%d (%.0f%%) | needs_review=%d (%.0f%%) | rejected=%d (%.0f%%)",
        total,
        elapsed,
        validated,
        100 * validated / total if total else 0,
        review,
        100 * review / total if total else 0,
        rejected,
        100 * rejected / total if total else 0,
    )
