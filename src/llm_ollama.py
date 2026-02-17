"""
Ollama LLM fallback client (Step 4).

Sends unresolved rows to a local Ollama instance for town extraction.
Uses temperature 0, strict JSON schema, and retry logic.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import requests

from . import config
from .preprocess import redact_pii
from .schemas import LLMResponse

logger = logging.getLogger(__name__)

# ── Prompt template ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a geographic address parser. Your task is to identify the town or \
city name from the provided address fields.

Rules:
1. Return ONLY a JSON object matching the schema below — no extra text.
2. Propose the most likely town/city name present in the address.
3. Do NOT invent, guess, or hallucinate a town name that is not implied \
by the address text.
4. If you cannot confidently identify a town, set "town_candidate" to null.
5. Consider the country_code to guide your interpretation.
6. The confidence value must be between 0.0 and 1.0.

Response JSON schema:
{
  "town_candidate": "string or null",
  "confidence": 0.0,
  "needs_manual_review": false
}
"""

_FEW_SHOT_EXAMPLES = [
    {
        "role": "user",
        "content": json.dumps(
            {
                "address_1": "Marienplatz 1",
                "address_2": "80331 München",
                "address_3": None,
                "country_code": "DE",
                "parser_warnings": ["libpostal_no_city_label"],
            }
        ),
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "town_candidate": "München",
                "confidence": 0.95,
                "needs_manual_review": False,
            }
        ),
    },
    {
        "role": "user",
        "content": json.dumps(
            {
                "address_1": "東京都渋谷区神宮前1-1",
                "address_2": None,
                "address_3": None,
                "country_code": "JP",
                "parser_warnings": ["libpostal_no_city_label"],
            }
        ),
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "town_candidate": "Shibuya",
                "confidence": 0.80,
                "needs_manual_review": True,
            }
        ),
    },
    {
        "role": "user",
        "content": json.dumps(
            {
                "address_1": "PO Box 1234",
                "address_2": None,
                "address_3": None,
                "country_code": "US",
                "parser_warnings": ["libpostal_no_city_label"],
            }
        ),
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "town_candidate": None,
                "confidence": 0.0,
                "needs_manual_review": True,
            }
        ),
    },
]


def _build_user_message(
    address_1: Optional[str],
    address_2: Optional[str],
    address_3: Optional[str],
    country_code: str,
    parser_warnings: list[str],
) -> str:
    """Build the user message payload for the LLM."""
    return json.dumps(
        {
            "address_1": address_1,
            "address_2": address_2,
            "address_3": address_3,
            "country_code": country_code,
            "parser_warnings": parser_warnings,
        },
        ensure_ascii=False,
    )


def _parse_llm_response(raw_text: str) -> LLMResponse:
    """
    Parse the raw LLM text output into a validated LLMResponse.

    Attempts to extract JSON from the response even if the LLM wraps it
    in markdown code fences or extra text.
    """
    text = raw_text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON object in the text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                raise ValueError(f"Cannot parse LLM response as JSON: {redact_pii(raw_text, keep_chars=20)}")
        else:
            raise ValueError(f"No JSON object found in LLM response: {redact_pii(raw_text, keep_chars=20)}")

    return LLMResponse(
        town_candidate=data.get("town_candidate"),
        confidence=float(data.get("confidence", 0.0)),
        needs_manual_review=bool(data.get("needs_manual_review", False)),
    )


def call_llm(
    address_1: Optional[str],
    address_2: Optional[str],
    address_3: Optional[str],
    country_code: str,
    parser_warnings: list[str],
) -> tuple[Optional[LLMResponse], list[str]]:
    """
    Call the Ollama LLM to extract a town candidate.

    Returns:
        A tuple of (LLMResponse or None, list of warnings).
        On unrecoverable failure, LLMResponse is None and warnings
        explain what went wrong.
    """
    warnings: list[str] = []
    user_content = _build_user_message(
        address_1, address_2, address_3, country_code, parser_warnings
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        *_FEW_SHOT_EXAMPLES,
        {"role": "user", "content": user_content},
    ]

    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": config.LLM_TEMPERATURE,
            "num_predict": config.LLM_MAX_TOKENS,
        },
    }

    url = f"{config.OLLAMA_BASE_URL}/api/chat"

    last_error: Optional[Exception] = None
    for attempt in range(1, config.LLM_MAX_RETRIES + 1):
        try:
            logger.debug(
                "LLM request attempt %d/%d for country=%s",
                attempt,
                config.LLM_MAX_RETRIES,
                country_code,
            )
            resp = requests.post(
                url,
                json=payload,
                timeout=config.LLM_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()

            body = resp.json()
            raw_text = body.get("message", {}).get("content", "")

            if not raw_text.strip():
                logger.warning("LLM returned empty content on attempt %d", attempt)
                last_error = ValueError("empty response")
                continue

            llm_response = _parse_llm_response(raw_text)

            # Cap confidence between 0 and 1
            llm_response.confidence = max(0.0, min(1.0, llm_response.confidence))

            if llm_response.confidence < 0.5:
                warnings.append("llm_low_confidence")

            return llm_response, warnings

        except requests.exceptions.Timeout:
            logger.warning("LLM timeout on attempt %d", attempt)
            last_error = TimeoutError("llm_timeout")

        except requests.exceptions.ConnectionError:
            logger.warning("LLM connection error on attempt %d", attempt)
            last_error = ConnectionError("llm_unavailable")

        except (ValueError, KeyError) as e:
            logger.warning("LLM parse error on attempt %d: %s", attempt, e)
            last_error = e

        except requests.exceptions.HTTPError as e:
            logger.warning("LLM HTTP error on attempt %d: %s", attempt, e)
            last_error = e

        # Exponential backoff: 1s, 2s, 4s
        if attempt < config.LLM_MAX_RETRIES:
            backoff = 2 ** (attempt - 1)
            logger.debug("Backing off %ds before retry", backoff)
            time.sleep(backoff)

    # All retries exhausted
    if isinstance(last_error, TimeoutError):
        warnings.append("llm_timeout")
    elif isinstance(last_error, ConnectionError):
        warnings.append("llm_unavailable")
    elif isinstance(last_error, (ValueError, KeyError)):
        warnings.append("llm_parse_error")
    else:
        warnings.append("llm_unavailable")

    logger.error("LLM fallback failed after %d retries: %s", config.LLM_MAX_RETRIES, last_error)
    return None, warnings
