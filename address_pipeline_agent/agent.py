"""
AddressPipelineAgent — Root orchestrator (V3.2 §7).

A CustomAgent that runs the 4 sub-agents in sequence with conditional
LLM skip logic:

    1. DeterministicResolver  (always)
    2. LlmAddressParser       (only if status == "unresolved")
    3. Revalidation            (always, except rejected)
    4. Persist                 (always)

Exports ``root_agent`` for ADK and ``batch_runner``.

When launched via ``adk web``, the user's chat message is parsed into
state keys (address_1, address_2, address_3, country_code) so the
pipeline can run without batch_runner pre-populating the session.

Expected chat formats (one per message):
    address_1 | address_2 | country_code
    address_1 | address_2 | address_3 | country_code
    address_1 | country_code
"""

from __future__ import annotations

import logging
import re
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from utils.prompts import build_instruction

from .sub_agents.deterministic_resolver.agent import deterministic_resolver_agent
from .sub_agents.llm_parser.agent import llm_parser_agent
from .sub_agents.persist.agent import persist_agent
from .sub_agents.revalidation.agent import revalidation_agent

logger = logging.getLogger(__name__)


# ── Chat-message → state parser (for adk web) ─────────────────────────────

def _parse_chat_message(text: str) -> dict[str, str] | None:
    """Parse a pipe-delimited chat message into address state keys.

    Returns a dict with address_1, address_2, address_3, country_code
    if the message looks like structured input, else None.

    Examples:
        "Musterstr. 5, 80331 | Munich | DE"
            → {address_1: "Musterstr. 5, 80331", address_2: "Munich",
               address_3: "", country_code: "DE"}
        "Musterstr. 5, 80331 | Munich | Bavaria | DE"
            → {address_1: "Musterstr. 5, 80331", address_2: "Munich",
               address_3: "Bavaria", country_code: "DE"}
    """
    if "|" not in text:
        return None

    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 2:
        return None

    # Last part should be a 2-letter country code
    candidate_cc = parts[-1].upper()
    if not re.match(r"^[A-Z]{2}$", candidate_cc):
        return None

    address_parts = parts[:-1]  # everything except country code
    return {
        "address_1": address_parts[0] if len(address_parts) >= 1 else "",
        "address_2": address_parts[1] if len(address_parts) >= 2 else "",
        "address_3": address_parts[2] if len(address_parts) >= 3 else "",
        "country_code": candidate_cc,
    }


class AddressPipelineAgent(BaseAgent):
    """Root orchestrator with conditional LLM routing."""

    # Declare sub_agents so ADK's Pydantic model picks them up
    deterministic: BaseAgent = deterministic_resolver_agent
    llm_parser: BaseAgent = llm_parser_agent
    revalidation: BaseAgent = revalidation_agent
    persist: BaseAgent = persist_agent

    model_config = {"arbitrary_types_allowed": True}

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        # ── Auto-parse chat input (adk web) ────────────────────────────
        # When running via `adk web`, address data arrives as a user
        # chat message, not pre-populated state keys.  Detect this and
        # parse the pipe-delimited message into state.
        if not state.get("address_1"):
            user_msg = self._extract_last_user_message(ctx)
            if user_msg:
                parsed = _parse_chat_message(user_msg)
                if parsed:
                    logger.info("Parsed chat input → %s", parsed)
                    for k, v in parsed.items():
                        state[k] = v
                else:
                    logger.warning(
                        "Chat message not pipe-delimited, cannot parse: %s",
                        user_msg[:80],
                    )

        row_key = f"{state.get('country_code', '??')}|{state.get('address_1', '?')[:30]}"
        logger.info("Pipeline start: %s", row_key)

        # ── Step 1: Deterministic resolver ─────────────────────────────
        async for event in self.deterministic.run_async(ctx):
            yield event

        # ── Step 2: LLM parser (conditional) ───────────────────────────
        if state.get("status") == "unresolved":
            logger.debug("Routing to LLM parser for: %s", row_key)

            # Inject address context into the LLM instruction
            self.llm_parser.instruction = build_instruction(state)

            try:
                async for event in self.llm_parser.run_async(ctx):
                    yield event
            except Exception as exc:
                logger.warning(
                    "LLM parser error for %s: %s — continuing without LLM",
                    row_key, exc,
                )
                state["warnings"] = state.get("warnings", [])
                if isinstance(state["warnings"], list):
                    state["warnings"].append("llm_parse_error")
                else:
                    state["warnings"] = [str(state["warnings"]), "llm_parse_error"]

            # After LLM: extract result to state if output_key did its job
            llm_result = state.get("llm_result")
            if llm_result and isinstance(llm_result, dict):
                # Populate town_candidate from LLM output for revalidation
                if llm_result.get("town"):
                    state["town_candidate"] = llm_result["town"]
                if llm_result.get("postal_code"):
                    state["libpostal_postal_code"] = (
                        state.get("libpostal_postal_code")
                        or llm_result["postal_code"]
                    )
                state["parser_source"] = "llm"
        else:
            logger.debug("Skipping LLM (status=%s): %s", state.get("status"), row_key)

        # ── Step 3: Revalidation ───────────────────────────────────────
        async for event in self.revalidation.run_async(ctx):
            yield event

        # ── Step 4: Persist ────────────────────────────────────────────
        async for event in self.persist.run_async(ctx):
            yield event

        logger.info(
            "Pipeline done: %s → status=%s, town=%s",
            row_key,
            state.get("final_result", {}).get("status"),
            state.get("final_result", {}).get("town"),
        )

        # ── Emit a human-readable reply (for adk web chat) ────────────
        fr = state.get("final_result", {})
        summary_lines = [
            f"**Status:** {fr.get('status', 'unknown')}",
            f"**Town:** {fr.get('town', '—')}",
            f"**Country:** {fr.get('country_code', '—')}",
            f"**Street:** {fr.get('street', '—')}",
            f"**Postal Code:** {fr.get('postal_code', '—')}",
            f"**Confidence:** {fr.get('confidence_score', 0)}",
        ]
        if fr.get("geonames_match"):
            summary_lines.append(f"**GeoNames ID:** {fr.get('geonames_id', '—')}")
        if fr.get("warnings"):
            summary_lines.append(f"**Warnings:** {fr.get('warnings', '—')}")
        if fr.get("review_reason"):
            summary_lines.append(f"**Review Reason:** {fr.get('review_reason', '—')}")

        reply_text = "\n".join(summary_lines)
        yield Event(
            author=self.name,
            actions=EventActions(state_delta={}),
            content=types.Content(
                parts=[types.Part(text=reply_text)],
                role="model",
            ),
        )

    # ── Helper: extract last user message from session events ──────────

    @staticmethod
    def _extract_last_user_message(ctx: InvocationContext) -> str | None:
        """Walk session events backwards to find the latest user text."""
        if not ctx.session or not ctx.session.events:
            return None
        for event in reversed(ctx.session.events):
            if (
                event.content
                and event.content.role == "user"
                and event.content.parts
            ):
                for part in event.content.parts:
                    if part.text and part.text.strip():
                        return part.text.strip()
        return None


# ── Module-level root agent (for ADK `adk web` and batch_runner) ──────────

root_agent = AddressPipelineAgent(
    name="address_pipeline",
    sub_agents=[
        deterministic_resolver_agent,
        llm_parser_agent,
        revalidation_agent,
        persist_agent,
    ],
)
