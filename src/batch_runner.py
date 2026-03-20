"""
Batch runner — CLI entry point for the address pipeline (V3.2 §9.5).

Two-pass hybrid architecture
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
**Pass 1 — Deterministic (pure Python, zero ADK overhead):**
    Runs Steps 0-5 as direct service-function calls on every row.
    Rows that resolve deterministically (~57%) proceed immediately to
    Steps 7-8 (revalidation + persist), also as direct calls.
    Cost: ~1-5 ms per row.

**Pass 2 — LLM-only (ADK sessions):**
    Only unresolved rows (~43%) get ADK sessions for the full LLM
    agent (Step 6) + revalidation (Step 7) + persist (Step 8).
    Cost: ~10-15 s per row (local Ollama).

This gives identical quality to the full ADK pipeline but avoids
creating sessions, events, and state-deltas for deterministic rows —
a 20-100× speedup for those rows.

Concurrency model
~~~~~~~~~~~~~~~~~
LLM rows are dispatched in **async batches** controlled by:
  --concurrency N   (default 4) — max LLM rows processed simultaneously.

For local Ollama, set OLLAMA_NUM_PARALLEL env var (default 1) on the
Ollama server to allow multiple concurrent inferences:
    OLLAMA_NUM_PARALLEL=4 ollama serve

For 32k-row production batches the recommended settings are:
    --concurrency 8 --batch-size 500

Checkpointing & crash recovery
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A rolling checkpoint file (``<output>.ckpt.csv``) is written after
each batch completes. If the process crashes, re-run with ``--resume``
to skip already-completed rows and continue from where it left off.

Usage:
    python -m src.batch_runner data/input/test_addresses.xlsx
    python -m src.batch_runner data/input/test_addresses.xlsx --concurrency 8
    python -m src.batch_runner data/input/big.csv --concurrency 16 --batch-size 500

    # Resume after a crash — picks up from last checkpoint:
    python -m src.batch_runner data/input/big.csv --resume
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from address_pipeline_agent.agent import root_agent
from services import (
    address_scanner,
    geonames_exact,
    geonames_revalidation,
    libpostal_parser,
    mismatch_detector,
    normalizer,
    persistence,
    postal_lookup,
)
from services.io_reader import read_input
from services.io_writer import write_output
from utils.config import CHECKPOINT_INTERVAL_ROWS, OUTPUT_DIR

logger = logging.getLogger("batch_runner")

APP_NAME = "address_pipeline"
USER_ID = "batch_user"

# Default concurrency — can be overridden via CLI or env
DEFAULT_CONCURRENCY = int(os.getenv("BATCH_CONCURRENCY", "4"))
DEFAULT_BATCH_SIZE = int(os.getenv("BATCH_SIZE", "200"))


# ── Checkpoint helpers ───────────────────────────────────────────────────────


def _checkpoint_path(output_file: Path) -> Path:
    """Return the rolling checkpoint path for a given output file."""
    return output_file.with_suffix(".ckpt.csv")


def _write_checkpoint(results: list[dict | None], ckpt_file: Path) -> int:
    """Write all non-None results to the checkpoint CSV.

    Each result dict is augmented with ``__row_index__`` (1-based) so
    the resume logic knows which rows are done. Returns the count of
    rows written.
    """
    valid = [(i, r) for i, r in enumerate(results) if r is not None]
    if not valid:
        return 0

    rows_to_write: list[dict] = []
    for idx, result in valid:
        row = dict(result)
        row["__row_index__"] = idx + 1  # 1-based, matches _process_row
        rows_to_write.append(row)

    df = pd.DataFrame(rows_to_write)
    df.to_csv(ckpt_file, index=False)
    return len(rows_to_write)


def _load_checkpoint(ckpt_file: Path) -> dict[int, dict]:
    """Load a checkpoint CSV and return {row_index: result_dict}.

    Returns an empty dict if the file doesn't exist or can't be read.
    """
    if not ckpt_file.exists():
        return {}

    try:
        df = pd.read_csv(ckpt_file, dtype=str, keep_default_na=False)
    except Exception as exc:
        logger.warning("Could not read checkpoint %s: %s", ckpt_file, exc)
        return {}

    if "__row_index__" not in df.columns:
        logger.warning("Checkpoint %s missing __row_index__ column — ignoring", ckpt_file)
        return {}

    loaded: dict[int, dict] = {}
    for _, row_data in df.iterrows():
        try:
            idx = int(row_data["__row_index__"])
        except (ValueError, TypeError):
            continue
        result = row_data.to_dict()
        del result["__row_index__"]
        # Restore numeric types for key fields
        for key in ("confidence_score", "geonames_id"):
            val = result.get(key)
            if val is not None and val != "":
                try:
                    result[key] = float(val) if key == "confidence_score" else int(float(val))
                except (ValueError, TypeError):
                    pass
        # Restore boolean fields
        for key in ("geonames_match", "mismatch_detected"):
            val = result.get(key)
            if isinstance(val, str):
                result[key] = val.lower() in ("true", "1", "yes")
        loaded[idx] = result

    return loaded


# ── Pass 1: Deterministic resolution (pure Python, no ADK) ──────────────────


def _resolve_deterministic(row: dict, row_index: int, job_id: str = "") -> dict:
    """Run Steps 0-5 + 7 + 8 as direct service calls.

    If the row resolves deterministically (exact match or scan match),
    the full result dict is returned with state["final_result"] populated.
    If unresolved, returns the partial state dict with status="unresolved"
    so the caller knows to route it to the LLM pass.

    This avoids all ADK overhead (session creation, event streaming,
    state-delta computation) for the ~57% of rows that don't need LLM.
    """
    state = {
        "address_1": row.get("address_1") or "",
        "address_2": row.get("address_2") or "",
        "address_3": row.get("address_3") or "",
        "country_code": (row.get("country_code") or "").strip().upper(),
        "row_index": row_index,
        "job_id": job_id,
        "warnings": [],
    }

    # Check for empty address
    has_address = any(
        state.get(f"address_{i}") and str(state.get(f"address_{i}")).strip()
        for i in range(1, 4)
    )
    if not has_address:
        state["status"] = "rejected"
        state["parser_source"] = None
        state["warnings"].append("no_address_data")
        logger.debug("Row %d: No address data — rejected (deterministic)", row_index)
        # Still need revalidation + persist for consistent output
        geonames_revalidation.revalidate(state)
        persistence.persist(state)
        return state

    # Step 0: Preprocess
    normalizer.preprocess(state)

    # Step 1: libpostal parse
    libpostal_parser.parse(state)

    # Country-only guard: address contains only a country name
    if state.get("libpostal_country") and not any([
        state.get("libpostal_town"),
        state.get("libpostal_street"),
        state.get("libpostal_building"),
        state.get("libpostal_postal_code"),
        state.get("libpostal_city_candidates"),
    ]):
        state["status"] = "needs_review"
        state["parser_source"] = None
        state["confidence"] = 0.0
        state["warnings"].append("country_only_address")
        logger.debug("Row %d: Country-only address — needs review", row_index)
        persistence.persist(state)
        return state

    # Step 2: Postal code lookup
    postal_lookup.lookup(state)

    # Step 3: GeoNames exact match
    geonames_exact.match(state)

    if state.get("exact_match"):
        state["status"] = "resolved"
        state["parser_source"] = "libpostal"
        logger.debug(
            "Row %d: Resolved deterministically (exact): %s",
            row_index, state.get("town_candidate"),
        )
        # Step 7 + 8: revalidation + persist
        geonames_revalidation.revalidate(state)
        persistence.persist(state)
        return state

    # Step 4: Mismatch detection
    mismatch_detector.detect(state)

    # Step 5: Address scan
    address_scanner.scan(state)

    if state.get("scan_match"):
        state["status"] = "resolved"
        state["parser_source"] = "geonames_scan"
        state["town_candidate"] = state.get("scan_candidate")
        logger.debug(
            "Row %d: Resolved deterministically (scan): %s",
            row_index, state.get("town_candidate"),
        )
        # Step 7 + 8: revalidation + persist
        geonames_revalidation.revalidate(state)
        persistence.persist(state)
        return state

    # Unresolved — needs LLM
    state["status"] = "unresolved"
    return state


# ── Pass 2: LLM processing via ADK (only for unresolved rows) ───────────────


async def _process_llm_row(
    runner: Runner,
    session_service: InMemorySessionService,
    row_index: int,
    state: dict,
    semaphore: asyncio.Semaphore,
) -> tuple[int, dict]:
    """Run the full ADK pipeline for a single unresolved row.

    The state dict already has Steps 0-5 completed from Pass 1.
    The ADK agent will run Step 6 (LLM) + Step 7 (revalidation) + Step 8 (persist).
    We pass the full pre-computed state so no deterministic work is repeated.
    """
    async with semaphore:
        session_id = f"row_{row_index:06d}"

        # Pass the full pre-computed state from Pass 1
        await session_service.create_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
            state=state,
        )

        trigger = types.Content(
            role="user",
            parts=[types.Part(text="Process this address.")],
        )

        # Run the agent pipeline — deterministic_resolver will see
        # status="unresolved" and the orchestrator routes to LLM
        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=session_id,
            new_message=trigger,
        ):
            pass

        # Re-fetch session to get the fully-updated state
        session = await session_service.get_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
        )

        final_result = session.state.get("final_result")
        if final_result:
            return (row_index, final_result)

        logger.warning("Row %d: no final_result in session state", row_index)
        return (row_index, {
            "input_address_1": state["address_1"],
            "input_address_2": state["address_2"],
            "input_address_3": state["address_3"],
            "country_code": state["country_code"],
            "address_line_1": "",
            "address_line_2": "",
            "status": "rejected",
            "confidence_score": 0.0,
            "warnings": "pipeline_error",
            "review_reason": "no_final_result",
        })


def _make_error_result(row: dict) -> dict:
    """Build a rejected result dict for a row that crashed."""
    return {
        "input_address_1": row.get("address_1"),
        "input_address_2": row.get("address_2"),
        "input_address_3": row.get("address_3"),
        "country_code": row.get("country_code"),
        "address_line_1": "",
        "address_line_2": "",
        "status": "rejected",
        "confidence_score": 0.0,
        "warnings": "pipeline_exception",
        "review_reason": "unhandled_exception",
    }


# ── Batch orchestrator ───────────────────────────────────────────────────────


async def run_batch(
    input_path: str,
    output_path: str | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    batch_size: int = DEFAULT_BATCH_SIZE,
    resume: bool = False,
) -> Path:
    """Run the full batch pipeline with async concurrency.

    Args:
        input_path:   Path to the input file (Excel or CSV).
        output_path:  Path to the output file.
        concurrency:  Max rows processed simultaneously.
        batch_size:   Rows per batch (checkpoint written between batches).
        resume:       If True, load the checkpoint file and skip rows
                      that were already processed.

    Returns:
        Path to the written output file.
    """
    # Single job_id for the entire batch run (V3.2 §8.1)
    job_id = str(uuid.uuid4())

    input_file = Path(input_path)
    if not input_file.exists():
        logger.error("Input file not found: %s", input_file)
        sys.exit(1)

    # Default output path — include a datetime stamp so each run is unique
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if output_path is None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_file = OUTPUT_DIR / f"{input_file.stem}_output_{timestamp}.csv"
    else:
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

    ckpt_file = _checkpoint_path(output_file)

    # Read input
    logger.info("Reading input: %s", input_file)
    logger.info("Job ID: %s", job_id)
    rows = read_input(str(input_file))
    total = len(rows)
    logger.info("Total rows: %d | concurrency: %d | batch_size: %d",
                total, concurrency, batch_size)

    # ── Resume from checkpoint ───────────────────────────────────
    results: list[dict | None] = [None] * total  # pre-allocate for ordered placement
    skipped = 0

    if resume:
        prior = _load_checkpoint(ckpt_file)
        if prior:
            for idx, result in prior.items():
                if 1 <= idx <= total:
                    results[idx - 1] = result
                    skipped += 1
            logger.info(
                "Resumed from checkpoint: %d/%d rows already done — %d remaining",
                skipped, total, total - skipped,
            )
        else:
            logger.info("No usable checkpoint found — starting from scratch")

    t0 = time.perf_counter()

    # ══════════════════════════════════════════════════════════════
    # PASS 1: Deterministic resolution (pure Python, no ADK)
    # ══════════════════════════════════════════════════════════════
    # Run Steps 0-5 + 7 + 8 as direct function calls for every
    # pending row. Resolved rows go straight into results[].
    # Unresolved rows are collected for the LLM pass.
    #
    # This is the key optimisation: ~57% of rows resolve here in
    # microseconds, avoiding all ADK session/event overhead.
    # ──────────────────────────────────────────────────────────────

    llm_queue: list[tuple[int, dict]] = []  # (row_index, partial_state)
    n_deterministic = 0
    n_rejected = 0

    logger.info("Pass 1: Deterministic resolution (%d pending rows) …", total - skipped)
    t_det = time.perf_counter()

    for i in range(total):
        if results[i] is not None:
            continue  # already done (checkpoint resume)

        row = rows[i]
        row_index = i + 1  # 1-based

        try:
            state = _resolve_deterministic(row, row_index, job_id)
        except Exception:
            logger.exception("Row %d: deterministic pass crashed", row_index)
            results[i] = _make_error_result(row)
            n_rejected += 1
            continue

        if state.get("status") == "unresolved":
            # Needs LLM — queue for Pass 2
            llm_queue.append((row_index, state))
        else:
            # Deterministic resolution complete — store final_result
            final_result = state.get("final_result")
            if final_result:
                results[i] = final_result
                n_deterministic += 1
            else:
                results[i] = _make_error_result(row)
                n_rejected += 1

    det_elapsed = time.perf_counter() - t_det
    logger.info(
        "Pass 1 complete: %d deterministic + %d rejected in %.1fs "
        "(%.0f rows/sec). %d rows queued for LLM.",
        n_deterministic, n_rejected, det_elapsed,
        (n_deterministic + n_rejected) / det_elapsed if det_elapsed > 0 else 0,
        len(llm_queue),
    )

    # ══════════════════════════════════════════════════════════════
    # PASS 2: LLM processing via ADK (only unresolved rows)
    # ══════════════════════════════════════════════════════════════
    # Only unresolved rows get ADK sessions. The pre-computed state
    # from Pass 1 is injected as session state so Steps 0-5 are NOT
    # re-run — the DeterministicResolverAgent will see the existing
    # state and the orchestrator routes directly to the LLM agent.
    # ──────────────────────────────────────────────────────────────

    if llm_queue:
        logger.info(
            "Pass 2: LLM resolution (%d rows, concurrency=%d, batch_size=%d) …",
            len(llm_queue), concurrency, batch_size,
        )

        session_service = InMemorySessionService()
        runner = Runner(
            app_name=APP_NAME,
            agent=root_agent,
            session_service=session_service,
        )
        semaphore = asyncio.Semaphore(concurrency)

        completed_llm = 0
        t_llm = time.perf_counter()

        # Process LLM rows in batches for checkpointing
        for batch_start in range(0, len(llm_queue), batch_size):
            batch = llm_queue[batch_start : batch_start + batch_size]

            logger.info(
                "LLM batch %d–%d of %d",
                batch_start + 1,
                min(batch_start + batch_size, len(llm_queue)),
                len(llm_queue),
            )

            tasks = []
            for row_index, state in batch:
                tasks.append(
                    asyncio.create_task(
                        _process_llm_row(
                            runner, session_service,
                            row_index, state, semaphore,
                        ),
                        name=f"llm-row-{row_index}",
                    )
                )

            task_results = await asyncio.gather(*tasks, return_exceptions=True)

            for j, task_result in enumerate(task_results):
                row_index, state = batch[j]
                global_idx = row_index - 1  # back to 0-based

                if isinstance(task_result, Exception):
                    logger.exception(
                        "Row %d LLM failed: %s", row_index, task_result,
                    )
                    results[global_idx] = _make_error_result(
                        {"address_1": state.get("address_1"),
                         "address_2": state.get("address_2"),
                         "address_3": state.get("address_3"),
                         "country_code": state.get("country_code")}
                    )
                else:
                    _, result_dict = task_result
                    results[global_idx] = result_dict

                completed_llm += 1

            # Progress for LLM pass
            llm_elapsed = time.perf_counter() - t_llm
            rate = completed_llm / llm_elapsed if llm_elapsed > 0 else 0
            logger.info(
                "LLM progress: %d/%d completed (%.2f rows/sec, elapsed %.1fs)",
                completed_llm, len(llm_queue), rate, llm_elapsed,
            )

            # Write rolling checkpoint after every LLM batch
            n_done = sum(1 for r in results if r is not None)
            if batch_start + batch_size < len(llm_queue):
                n_saved = _write_checkpoint(results, ckpt_file)
                logger.info(
                    "Checkpoint saved: %s (%d rows). "
                    "To resume after a crash: --resume -o %s",
                    ckpt_file, n_saved, output_file,
                )

        llm_total_elapsed = time.perf_counter() - t_llm
        logger.info(
            "Pass 2 complete: %d LLM rows in %.1fs (%.2f rows/sec)",
            len(llm_queue), llm_total_elapsed,
            len(llm_queue) / llm_total_elapsed if llm_total_elapsed > 0 else 0,
        )
    else:
        logger.info("Pass 2: No LLM rows — all resolved deterministically!")

    # ── Write final output ───────────────────────────────────────
    # Replace any remaining Nones with error results (should not happen)
    for i in range(total):
        if results[i] is None:
            logger.warning("Row %d: no result after all batches — marking rejected", i + 1)
            results[i] = _make_error_result(rows[i])

    final_results: list[dict] = [r for r in results if r is not None]

    elapsed = time.perf_counter() - t0
    output_written = write_output(final_results, str(output_file))
    logger.info(
        "Done: %d rows (%d new + %d resumed) in %.1fs (%.1f rows/sec). Output: %s",
        total, total - skipped, skipped,
        elapsed, (total - skipped) / elapsed if elapsed > 0 else 0,
        output_written,
    )

    # ── Batch summary: deterministic vs LLM, token usage ────────
    n_llm = sum(1 for r in final_results if r.get("llm_calls", 0) > 0)
    n_deterministic = sum(
        1 for r in final_results
        if r.get("parser_source") and r["parser_source"] != "llm"
    )
    n_rejected = sum(
        1 for r in final_results if r.get("status") == "rejected"
    )
    total_llm_calls = sum(r.get("llm_calls", 0) for r in final_results)
    total_prompt = sum(r.get("llm_prompt_tokens", 0) for r in final_results)
    total_completion = sum(r.get("llm_completion_tokens", 0) for r in final_results)
    total_tokens = total_prompt + total_completion
    logger.info("=" * 60)
    logger.info("BATCH SUMMARY")
    logger.info("  Total rows:        %d", total)
    logger.info("  Deterministic:     %d (%.0f%%)", n_deterministic,
                100 * n_deterministic / total if total else 0)
    logger.info("  LLM-assisted:      %d (%.0f%%)", n_llm,
                100 * n_llm / total if total else 0)
    logger.info("  Rejected/empty:    %d", n_rejected)
    logger.info("  LLM calls total:   %d", total_llm_calls)
    logger.info("  Tokens (prompt):   %d", total_prompt)
    logger.info("  Tokens (complet.): %d", total_completion)
    logger.info("  Tokens (total):    %d", total_tokens)
    if n_llm > 0:
        logger.info("  Avg calls/LLM row: %.1f", total_llm_calls / n_llm)
        logger.info("  Avg tokens/LLM row:%.0f", total_tokens / n_llm)
    logger.info("=" * 60)

    # Clean up checkpoint file after successful completion
    if ckpt_file.exists():
        ckpt_file.unlink()
        logger.info("Checkpoint removed: %s", ckpt_file)

    return output_written


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Run the address pipeline on a batch file."
    )
    parser.add_argument(
        "input_file",
        help="Path to the input file (Excel or CSV).",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Path to the output file. Default: data/output/<input>_output.csv",
    )
    parser.add_argument(
        "-c", "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Max rows processed concurrently (default: {DEFAULT_CONCURRENCY}).",
    )
    parser.add_argument(
        "-b", "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per batch for checkpointing (default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--loglevel",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Resume from last checkpoint. Requires -o to match the "
             "original output path so the checkpoint file can be found.",
    )

    args = parser.parse_args()

    log_level = getattr(logging, args.loglevel)
    log_fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    date_fmt = "%H:%M:%S"

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(log_fmt, datefmt=date_fmt))

    # File handler — timestamped log in project-root logs/
    project_root = Path(__file__).resolve().parent.parent
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"batch_{ts}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)  # INFO-level detail in file
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logging.basicConfig(
        level=log_level,
        handlers=[console_handler, file_handler],
    )

    # Silence noisy third-party loggers — only warnings and above
    for noisy in ("LiteLLM", "litellm", "httpx", "httpcore", "openai",
                  "google.adk", "google.genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger.info("Log file: %s", log_file)
    logger.info(
        "Settings: concurrency=%d, batch_size=%d, llm_concurrency=%s, loglevel=%s",
        args.concurrency, args.batch_size,
        os.getenv("LLM_CONCURRENCY", "1"),
        args.loglevel,
    )

    asyncio.run(run_batch(
        args.input_file, args.output, args.concurrency, args.batch_size,
        resume=args.resume,
    ))


if __name__ == "__main__":
    main()
