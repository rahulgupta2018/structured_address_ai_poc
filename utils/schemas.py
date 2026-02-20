"""
Pydantic models and data contracts for the address pipeline.

Shared by both services/ and agents/. Zero ADK dependency.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ── Enums ────────────────────────────────────────────────────────────────────


class Status(str, Enum):
    VALIDATED = "validated"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class ParserSource(str, Enum):
    LIBPOSTAL = "libpostal"
    GEONAMES_SCAN = "geonames_scan"
    LLM = "llm"


# ── Input ────────────────────────────────────────────────────────────────────


class AddressInput(BaseModel):
    """A single row from the input file (Excel / CSV)."""

    address_1: Optional[str] = None
    address_2: Optional[str] = None
    address_3: Optional[str] = None
    country_code: str = Field(..., min_length=2, max_length=2)

    @field_validator("country_code")
    @classmethod
    def uppercase_country_code(cls, v: str) -> str:
        return v.strip().upper()

    @property
    def has_address(self) -> bool:
        return any(
            line and line.strip()
            for line in [self.address_1, self.address_2, self.address_3]
        )

    @property
    def address_lines(self) -> list[str]:
        return [
            line.strip()
            for line in [self.address_1, self.address_2, self.address_3]
            if line and line.strip()
        ]


# ── GeoNames match result ───────────────────────────────────────────────────


class GeoNamesMatch(BaseModel):
    """Result of a GeoNames lookup."""

    matched: bool = False
    geonames_id: Optional[int] = None
    matched_name: Optional[str] = None
    match_type: Optional[str] = None  # "primary", "ascii", "alternate", "fuzzy"
    confidence: float = 0.0


# ── LLM structured output ───────────────────────────────────────────────────


class LlmAddressOutput(BaseModel):
    """Structured output schema for the LLM address parser agent.

    This is the output_schema that ADK's LlmAgent will enforce on the
    LLM response (V3.2 §6.2).

    Made lenient to handle local LLM quirks (Ollama):
    - ``confidence`` accepts any number and clamps to [0.0, 1.0].
    - ``town`` and ``reasoning`` default to "" so partial responses
      don't cause a hard ValidationError that crashes the pipeline.
    """

    town: Optional[str] = Field(default="", description="The resolved town/city name")
    postal_code: Optional[str] = Field(
        default=None, description="Postal/ZIP code if identified"
    )
    confidence: float = Field(
        default=0.0, description="Confidence score 0.0–1.0"
    )
    reasoning: Optional[str] = Field(
        default="", description="Brief explanation of how the town was determined"
    )

    @field_validator("town", "reasoning", mode="before")
    @classmethod
    def coerce_none_to_empty(cls, v: Any) -> str:
        """Accept None from the LLM and coerce to empty string."""
        return v if v is not None else ""
    suggested_country_code: Optional[str] = Field(
        default=None,
        description="If the town belongs to a different country than the input "
        "country_code, provide the correct ISO 3166-1 alpha-2 code here.",
    )

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, v: Any) -> float:
        """Accept any numeric-like value and clamp to [0, 1]."""
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        if v > 1.0:
            v = v / 100.0  # handle 0–100 scale
        return max(0.0, min(1.0, v))


class LLMResponse(BaseModel):
    """Legacy LLM response model (kept for compatibility during migration)."""

    town_candidate: Optional[str] = None
    confidence: float = 0.0
    needs_manual_review: bool = False


# ── Output ───────────────────────────────────────────────────────────────────


class AddressOutput(BaseModel):
    """A single row of the pipeline output."""

    # Original input (preserved for audit)
    address_1: Optional[str] = None
    address_2: Optional[str] = None
    address_3: Optional[str] = None
    country_code: str

    # Extracted structured fields
    town: Optional[str] = None
    street: Optional[str] = None
    building: Optional[str] = None
    postal_code: Optional[str] = None

    # Pipeline metadata
    status: Status = Status.REJECTED
    confidence_score: float = 0.0
    parser_source: Optional[ParserSource] = None
    geonames_match: bool = False
    geonames_id: Optional[int] = None
    normalized_town: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    review_reason: Optional[str] = None

    # Country mismatch fields
    mismatch_detected: bool = False
    suggested_country_code: Optional[str] = None

    @classmethod
    def from_input(cls, inp: AddressInput) -> "AddressOutput":
        return cls(
            address_1=inp.address_1,
            address_2=inp.address_2,
            address_3=inp.address_3,
            country_code=inp.country_code,
        )


# ── Session state key constants ──────────────────────────────────────────────

# Input keys (set by batch_runner / io_reader before agent runs)
STATE_ADDRESS_1 = "address_1"
STATE_ADDRESS_2 = "address_2"
STATE_ADDRESS_3 = "address_3"
STATE_COUNTRY_CODE = "country_code"

# Intermediate keys (set by DeterministicResolverAgent)
STATE_RAW_ADDRESS = "raw_address"
STATE_NORMALIZED = "normalized"
STATE_LIBPOSTAL_TOWN = "libpostal_town"
STATE_LIBPOSTAL_POSTAL_CODE = "libpostal_postal_code"
STATE_LIBPOSTAL_STREET = "libpostal_street"
STATE_LIBPOSTAL_BUILDING = "libpostal_building"
STATE_POSTAL_TOWN_CANDIDATE = "postal_town_candidate"
STATE_EXACT_MATCH = "exact_match"
STATE_GEONAMES_ID = "geonames_id"
STATE_TOWN_CANDIDATE = "town_candidate"
STATE_MATCH_TYPE = "match_type"
STATE_MATCH_CONFIDENCE = "match_confidence"
STATE_MISMATCH_DETECTED = "mismatch_detected"
STATE_SUGGESTED_CC = "suggested_country_code"
STATE_SCAN_MATCH = "scan_match"
STATE_SCAN_CANDIDATE = "scan_candidate"
STATE_STATUS = "status"
STATE_PARSER_SOURCE = "parser_source"
STATE_WARNINGS = "warnings"

# LLM keys (set by LlmAddressParserAgent)
STATE_LLM_RESULT = "llm_result"

# Revalidation keys
STATE_CONFIDENCE = "confidence"

# Output key (set by PersistAgent)
STATE_FINAL_RESULT = "final_result"
