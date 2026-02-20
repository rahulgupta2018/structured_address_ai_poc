"""
PersistAgent — Step 8.

A CustomAgent that assembles the final result dict from session state.
Always runs as the last agent in the pipeline.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from services import persistence

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


class PersistAgent(BaseAgent):
    """Step 8: assemble final result from session state."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        ri = state.get("row_index", "?")
        snapshot = dict(state)

        persistence.persist(state)

        result = state.get("final_result", {})
        town = result.get("town", "N/A")
        status = result.get("status", "unknown")
        logger.debug("Row %s: Persist → town=%s, status=%s", ri, town, status)

        yield _make_event(
            self.name,
            f"Result: town={town}, status={status}",
            _compute_delta(snapshot, state),
        )


persist_agent = PersistAgent(
    name="persist",
)
