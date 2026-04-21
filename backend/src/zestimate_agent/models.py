"""Pydantic models and graph state for every stage of the pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# ---------------------------------------------------------------------------
# Stage 1: Normalize
# ---------------------------------------------------------------------------


class NormalizedAddress(BaseModel):
    """Canonical US address produced by the normalize stage."""

    model_config = ConfigDict(str_strip_whitespace=True)

    street_number: str
    street_name: str
    unit: str | None = None
    city: str
    state: str = Field(..., min_length=2, max_length=2)
    zip5: str = Field(..., pattern=r"^\d{5}$")
    zip4: str | None = Field(default=None, pattern=r"^\d{4}$")
    lat: float | None = None
    lon: float | None = None
    confidence: Confidence

    @field_validator("state")
    @classmethod
    def _upper_state(cls, v: str) -> str:
        return v.upper()

    def single_line(self) -> str:
        """Render as a one-line canonical string (used for logging and cache keys)."""
        street = f"{self.street_number} {self.street_name}"
        if self.unit:
            street = f"{street} {self.unit}"
        zip_part = self.zip5 + (f"-{self.zip4}" if self.zip4 else "")
        return f"{street}, {self.city}, {self.state} {zip_part}".upper()


# ---------------------------------------------------------------------------
# Stage 2: Resolve
# ---------------------------------------------------------------------------


class Candidate(BaseModel):
    """A property match returned by a provider's search endpoint."""

    zpid: str
    street_number: str | None = None
    street_name: str | None = None
    unit: str | None = None
    city: str | None = None
    state: str | None = None
    zip5: str | None = None
    lat: float | None = None
    lon: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


class ResolvedProperty(BaseModel):
    zpid: str
    matched_address: str
    confidence: Confidence


# ---------------------------------------------------------------------------
# Stage 3: Fetch
# ---------------------------------------------------------------------------


class PropertyDetail(BaseModel):
    """Raw property detail extracted by a provider's get_property call."""

    zpid_echo: str
    zestimate: int | None
    rent_zestimate: int | None = None
    last_updated: datetime | None = None
    full_address: str
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Stage 4: Validate → final result
# ---------------------------------------------------------------------------


class ZestimateResult(BaseModel):
    """Successful, validated output of the pipeline."""

    address: str
    zestimate: int
    zpid: str
    fetched_at: datetime
    provider_used: str
    confidence: Confidence


# ---------------------------------------------------------------------------
# Agent-layer types
# ---------------------------------------------------------------------------


class DisambiguationChoice(BaseModel):
    """Structured output returned by the Mistral LLM at the disambiguate node."""

    chosen_zpid: str
    confidence: Confidence
    reasoning: str


class ClarificationRequest(BaseModel):
    """Terminal output when the agent cannot proceed without user input."""

    reason: str
    original_input: str
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    zpid: str | None = None  # set for NoZestimateError so API can return 404 + zpid


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ZestimateAgentError(Exception):
    """Base class for agent errors."""


class AmbiguousAddressError(ZestimateAgentError):
    def __init__(self, message: str, candidates: list[Any] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


class ValidationError(ZestimateAgentError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class NoZestimateError(ZestimateAgentError):
    """Raised when the correct property was found but Zillow has no Zestimate for it.

    Common causes: rental-only listings, new construction, commercial properties,
    or properties recently added to Zillow's database.
    """

    def __init__(self, message: str, zpid: str | None = None) -> None:
        super().__init__(message)
        self.zpid = zpid


class ProviderError(ZestimateAgentError):
    """Raised by providers on network / HTTP / empty-response failure."""


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


class GraphState(TypedDict, total=False):
    """Shared state passed between LangGraph nodes.

    Keys are optional (total=False) so nodes can return partial updates;
    LangGraph merges them into the running state.
    """

    input_address: str
    normalized: NormalizedAddress | None
    candidates: list[Candidate]
    resolved: ResolvedProperty | None
    property_detail: PropertyDetail | None
    result: ZestimateResult | None
    clarification: ClarificationRequest | None
    provider_used: str | None
    errors: list[str]
    messages: list[Any]  # list[BaseMessage] at runtime; Any to avoid import cycle
    attempt: int
    failed_at: str | None  # which stage set the last error ("normalize"|"resolve"|"fetch")
    graph_path: list[str]
