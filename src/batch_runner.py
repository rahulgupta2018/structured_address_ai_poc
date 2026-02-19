"""
Batch runner — CLI entry point for the address pipeline (V3.2 §9.5).

Reads an input file (Excel / CSV), runs each row through the ADK
agent pipeline **concurrently**, and writes the results to an output file.

Concurrency model
~~~~~~~~~~~~~~~~~
Rows are dispatched in **async batches** controlled by:
  --concurrency N   (default 4) — max rows processed simultaneously.

Each row gets its own ADK session, so they are fully independent.
Deterministic-only rows (~60%) complete in <1s and don't need the LLM.
LLM rows (~40%) take 10-15s but run in parallel (up to the semaphore
limit).

For local Ollama, set OLLAMA_NUM_PARALLEL env var (default 1) on the
Ollama server to allow multiple concurrent inferences:
    OLLAMA_NUM_PARALLEL=4 ollama serve

For 32k-row production batches the recommended settings are:
    --concurrency 8 --batch-size 500

Usage:
    python batch_runner.py data/input/test_addresses.xlsx
    python batch_runner.py data/input/test_addresses.xlsx --concurrency 8
    python batch_runner.py data/input/big.csv --concurrency 16 --batch-size 500
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from address_pipeline_agent.agent import root_agent
from services.io_reader import read_input
from services.io_writer import write_output
from utils.config import CHECKPOINT_INTERVAL_ROWS, OUTPUT_DIR

logger = logging.getLogger("batch_runner")

APP_NAME = "address_pipeline"
USER_ID = "batch_user"

# Default concurrency — can be overridden via CLI or env
DEFAULT_CONCURRENCY = int(os.getenv("BATCH_CONCURRENCY", "4"))
DEFAULT_BATCH_SIZE = int(os.getenv("BATCH_SIZE", "200"))


# ── Per-row processing ───────────────────────────────────────────────────────


async def _process_row(
    runner: Runner,
    session_service: InMemorySessionService,
    row_index: int,
    row: dict,
    semaphore: asyncio.Semaphore,
    job_id: str = "",
) -> tuple[int, dict]:
    """Process a single address row through the full agent pipeline.

    Uses a semaphore to limit concurrency across all in-flight rows.
    Returns (row_index, result_dict) so the caller can place results
    in order.
    """
    async with semaphore:
        session_id = f"row_{row_index:06d}"

        initial_state = {
            "address_1": row.get("address_1") or "",
            "address_2": row.get("address_2") or "",
            "address_3": row.get("address_3") or "",
            "country_code": (row.get("country_code") or "").strip().upper(),
            "row_index": row_index,
            "job_id": job_id,
            "warnings": [],
        }

        await session_service.create_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
            state=initial_state,
        )

        trigger = types.Content(
            role="user",
            parts=[types.Part(text="Process this address.")],
        )

        # Run the agent pipeline
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
            "address_1": initial_state["address_1"],
            "address_2": initial_state["address_2"],
            "address_3": initial_state["address_3"],
            "country_code": initial_state["country_code"],
            "status": "rejected",
            "confidence_score": 0.0,
            "warnings": "pipeline_error",
            "review_reason": "no_final_result",
        })


def _make_error_result(row: dict) -> dict:
    """Build a rejected result dict for a row that crashed."""
    return {
        "address_1": row.get("address_1"),
        "address_2": row.get("address_2"),
        "address_3": row.get("address_3"),
        "country_code": row.get("country_code"),
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
) -> Path:
    """Run the full batch pipeline with async concurrency.

    Args:
        input_path:   Path to the input file (Excel or CSV).
        output_path:  Path to the output file.
        concurrency:  Max rows processed simultaneously.
        batch_size:   Rows per batch (checkpoint written between batches).

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

    # Read input
    logger.info("Reading input: %s", input_file)
    logger.info("Job ID: %s", job_id)
    rows = read_input(str(input_file))
    total = len(rows)
    logger.info("Total rows: %d | concurrency: %d | batch_size: %d",
                total, concurrency, batch_size)

    # Set up ADK runner
    session_service = InMemorySessionService()
    runner = Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=session_service,
    )

    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict] = [None] * total  # pre-allocate for ordered placement
    completed = 0
    t0 = time.perf_counter()

    # Process in batches so we can checkpoint between them
    for batch_start in range(0, total, batch_size):
        batch_end = min(batch_start + batch_size, total)
        batch_rows = rows[batch_start:batch_end]

        logger.info(
            "Batch %d–%d of %d (size %d)",
            batch_start + 1, batch_end, total, len(batch_rows),
        )

        # Launch all rows in this batch concurrently
        tasks = []
        for i, row in enumerate(batch_rows):
            idx = batch_start + i  # 0-based global index
            tasks.append(
                asyncio.create_task(
                    _process_row(runner, session_service, idx + 1, row, semaphore, job_id),
                    name=f"row-{idx + 1}",
                )
            )

        # Gather results — exceptions are returned, not raised
        task_results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, task_result in enumerate(task_results):
            global_idx = batch_start + i
            row = rows[global_idx]

            if isinstance(task_result, Exception):
                logger.exception(
                    "Row %d failed: %s", global_idx + 1, task_result,
                )
                results[global_idx] = _make_error_result(row)
            else:
                row_index, result_dict = task_result
                results[global_idx] = result_dict

            completed += 1

        # Progress
        elapsed = time.perf_counter() - t0
        rate = completed / elapsed if elapsed > 0 else 0
        logger.info(
            "Progress: %d/%d completed (%.1f rows/sec, elapsed %.1fs)",
            completed, total, rate, elapsed,
        )

        # Checkpoint between batches (not after the last one)
        if batch_end < total and completed >= CHECKPOINT_INTERVAL_ROWS:
            ckpt_path = output_file.with_suffix(f".ckpt_{completed}.csv")
            valid_results = [r for r in results if r is not None]
            write_output(valid_results, str(ckpt_path))
            logger.info("Checkpoint: %s (%d rows)", ckpt_path, len(valid_results))

    # Write final output
    elapsed = time.perf_counter() - t0
    output_written = write_output(results, str(output_file))
    logger.info(
        "Done: %d rows in %.1fs (%.1f rows/sec). Output: %s",
        total, elapsed, total / elapsed if elapsed > 0 else 0, output_written,
    )

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
    ))


if __name__ == "__main__":
    main()
