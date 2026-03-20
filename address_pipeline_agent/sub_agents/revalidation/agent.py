"""
RevalidationAgent — Step 7.

A CustomAgent that re-validates the resolved town against GeoNames.
Runs only for LLM-path rows (status != "resolved" after deterministic steps).
Deterministic paths (Flow 1 / Flow 2) skip this step entirely — their
confidence is set by DeterministicResolverAgent.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from services import geonames_revalidation

logger = logging.getLogger(__name__)


def _compute_delta(before: dict, after: dict) -> dict:
    delta = {}
    for key, value in after.items():
        if key not in before or before[key] != value:
            delta[key] = value
    return delta


def _make_event(author: str, text: str, state_delta: dict | None = None) -> Event:
    return Event(
        author=author,
        actions=EventActions(state_delta=state_delta or {}),
        content=types.Content(
            role="model",
            parts=[types.Part(text=text)],
        ),
    )


class RevalidationAgent(BaseAgent):
    """Step 7: re-validate resolved town against GeoNames."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        ri = state.get("row_index", "?")
        snapshot = dict(state)

        # Skip revalidation for rejected rows
        if state.get("status") == "rejected":
            logger.debug("Row %s: Skipped revalidation — rejected", ri)
            yield _make_event(self.name, "Skipped — row rejected.")
            return

        geonames_revalidation.revalidate(state)

        confidence = state.get("confidence", 0.0)
        status = state.get("status", "unknown")
        logger.debug("Row %s: Revalidation complete: status=%s, confidence=%.2f", ri, status, confidence)

        yield _make_event(
            self.name,
            f"Revalidation complete: status={status}, confidence={confidence:.2f}",
            _compute_delta(snapshot, state),
        )


revalidation_agent = RevalidationAgent(
    name="revalidation",
)
