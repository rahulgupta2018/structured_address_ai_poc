"""
CLI entry point for the Structured Address AI pipeline.

Usage:
    python -m src.main --input data/samples/test_addresses.xlsx
    python -m src.main --input data/samples/test_addresses.xlsx --output data/output/result.xlsx
    python -m src.main --input data/samples/test_addresses.xlsx --skip-llm
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import config
from .pipeline import run


def _validate_output_path(output_path: str | None) -> str | None:
    """Ensure the output path stays within the project directory."""
    if output_path is None:
        return None
    resolved = Path(output_path).resolve()
    try:
        resolved.relative_to(config.PROJECT_ROOT)
    except ValueError:
        print(
            f"❌ Output path must be within the project directory "
            f"({config.PROJECT_ROOT}), got: {resolved}",
            file=sys.stderr,
        )
        sys.exit(1)
    return str(resolved)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Structured Address AI Pipeline — ISO 20022 town extraction"
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to the input Excel file (.xlsx)",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Path for the output Excel file (default: data/output/<stem>_output.xlsx)",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        default=False,
        help="Skip the LLM fallback step (deterministic-only mode)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args()

    # --- logging setup: console + rotating file ---
    log_level = getattr(logging, args.log_level)
    log_format = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    date_format = "%H:%M:%S"

    # Console handler
    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
    )

    # File handler — logs/<date>_<time>.log  (always DEBUG for full trace)
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_handler = logging.FileHandler(log_dir / f"run_{ts}.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    logging.getLogger().addHandler(file_handler)

    try:
        validated_output = _validate_output_path(args.output)
        results = run(
            input_path=args.input,
            output_path=validated_output,
            skip_llm=args.skip_llm,
        )
        validated = sum(1 for r in results if r.status.value == "validated")
        print(f"\n✅ Done — {len(results)} rows processed, {validated} validated.")
    except FileNotFoundError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logging.getLogger(__name__).exception("Pipeline failed")
        print(f"❌ Pipeline failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
