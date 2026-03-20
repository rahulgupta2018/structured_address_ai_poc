"""
DeterministicResolverAgent — Steps 0–5.

A CustomAgent (BaseAgent subclass) that runs the deterministic resolution
pipeline: preprocess → libpostal → postal lookup → exact match →
mismatch detect → address scan.

If resolved, sets state["status"] = "resolved" so the orchestrator
skips the LLM agent. If unresolved, sets state["status"] = "unresolved".
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from services import (
    address_scanner,
    geonames_exact,
    libpostal_parser,
    mismatch_detector,
    normalizer,
    postal_lookup,
)

logger = logging.getLogger(__name__)


def _compute_delta(before: dict, after: dict) -> dict:
    """Compute state_delta: keys that changed or were added."""
    delta = {}
    for key, value in after.items():
        if key not in before or before[key] != value:
            delta[key] = value
    return delta


def _make_event(author: str, text: str, state_delta: dict | None = None) -> Event:
    """Helper to build an Event with a text message and optional state delta."""
    return Event(
        author=author,
        actions=EventActions(state_delta=state_delta or {}),
        content=types.Content(
            role="model",
            parts=[types.Part(text=text)],
        ),
    )


class DeterministicResolverAgent(BaseAgent):
    """Steps 0–5: deterministic address resolution via plain service calls."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        ri = state.get("row_index", "?")

        # ── Fast exit: skip if Pass 1 already ran deterministic steps ──
        # When batch_runner's two-pass architecture pre-computes Steps 0-5
        # and injects the result as session state, we detect this via
        # status=unresolved + raw_address already populated.  No need to
        # re-run the deterministic pipeline.
        if state.get("status") == "unresolved" and state.get("raw_address"):
            logger.debug(
                "Row %s: Deterministic steps already completed (two-pass). Skipping.",
                ri,
            )
            yield _make_event(
                self.name,
                "Deterministic steps already done — skipping to LLM.",
            )
            return

        # Snapshot state before mutations so we can compute a delta
        snapshot = dict(state)

        # Ensure warnings list exists
        state.setdefault("warnings", [])

        # Check for empty address
        has_address = any(
            state.get(f"address_{i}") and str(state.get(f"address_{i}")).strip()
            for i in range(1, 4)
        )
        if not has_address:
            state["status"] = "rejected"
            state["parser_source"] = None
            state["warnings"].append("no_address_data")
            logger.info("Row %s: No address data — rejected", ri)
            yield _make_event(
                self.name,
                "No address data — rejected.",
                _compute_delta(snapshot, state),
            )
            return

        # Step 0: Preprocess
        normalizer.preprocess(state)

        # Step 1: libpostal parse
        libpostal_parser.parse(state)

        # Step 2: Postal code lookup
        postal_lookup.lookup(state)

        # Step 3: GeoNames exact match
        # (Steps 3 and 5 now handle mismatch_detected internally by trying
        # suggested_country_code first, then falling back to country_code.)
        geonames_exact.match(state)

        if state.get("exact_match"):
            state["status"] = "resolved"
            state["parser_source"] = "libpostal"
            state["confidence"] = state.get("match_confidence", 0.0)
            logger.debug(
                "Row %s: Resolved deterministically (exact): %s",
                ri, state.get("town_candidate"),
            )
            yield _make_event(
                self.name,
                f"Resolved (exact): {state.get('town_candidate')}",
                _compute_delta(snapshot, state),
            )
            return

        # Step 4: Mismatch detection
        mismatch_detector.detect(state)

        # Step 5: Address scan
        address_scanner.scan(state)

        if state.get("scan_match"):
            state["status"] = "resolved"
            state["parser_source"] = "geonames_scan"
            state["town_candidate"] = state.get("scan_candidate")
            state["confidence"] = state.get("match_confidence", 0.0)
            logger.debug(
                "Row %s: Resolved deterministically (scan): %s",
                ri, state.get("town_candidate"),
            )
            yield _make_event(
                self.name,
                f"Resolved (scan): {state.get('town_candidate')}",
                _compute_delta(snapshot, state),
            )
            return

        # Unresolved → LLM will be invoked
        state["status"] = "unresolved"
        yield _make_event(
            self.name,
            "Unresolved — needs LLM fallback.",
            _compute_delta(snapshot, state),
        )


# Module-level instance for import by orchestrator
deterministic_resolver_agent = DeterministicResolverAgent(
    name="deterministic_resolver",
)
