"""
Pydantic models for input/output data contracts.
Enforces schema validation at the boundaries of the pipeline.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

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
    """A single row from the input Excel file."""

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
        """True if at least one address line is non-empty."""
        return any(
            line and line.strip()
            for line in [self.address_1, self.address_2, self.address_3]
        )

    @property
    def address_lines(self) -> list[str]:
        """Return non-null, non-empty address lines in order."""
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


# ── LLM response ────────────────────────────────────────────────────────────


class LLMResponse(BaseModel):
    """Expected JSON response from the LLM fallback."""

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

    @classmethod
    def from_input(cls, inp: AddressInput) -> "AddressOutput":
        """Create an output row pre-populated with the original input fields."""
        return cls(
            address_1=inp.address_1,
            address_2=inp.address_2,
            address_3=inp.address_3,
            country_code=inp.country_code,
        )
