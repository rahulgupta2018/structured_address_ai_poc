"""Shared helper for printing step-by-step test diagnostics.

Usage in any test:
    from tests.test_services.report import report
    report("input", {"raw_address": "123 Main St"})
    report("output", {"normalized": "123 main st"})

Run with:  pytest -v -s   (the -s flag disables output capture)
"""

from __future__ import annotations

import json

_SEPARATOR = "─" * 60


def report(label: str, data, indent: int = 2) -> None:
    """Print a labelled key→value block for test diagnostics."""
    prefix = " " * indent
    print()  # blank line for readability
    print(f"{prefix}┌─ {label.upper()}")
    if isinstance(data, dict):
        for k, v in data.items():
            print(f"{prefix}│  {k}: {_fmt(v)}")
    elif isinstance(data, (list, tuple, set)):
        for item in data:
            print(f"{prefix}│  {_fmt(item)}")
    else:
        print(f"{prefix}│  {_fmt(data)}")
    print(f"{prefix}└{_SEPARATOR}")


def _fmt(value) -> str:
    """Format a value for display — truncate long content."""
    if value is None:
        return "None"
    if isinstance(value, str):
        if len(value) > 120:
            return repr(value[:120]) + "…"
        return repr(value)
    if isinstance(value, (dict, list)):
        s = json.dumps(value, default=str, ensure_ascii=False)
        if len(s) > 200:
            return s[:200] + "…"
        return s
    return repr(value)
