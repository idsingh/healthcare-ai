"""The LLM I/O contract.

Deliberately NOT the domain model. The model is asked for a narrow, quote-bearing
shape it can actually satisfy; the core then maps it into the domain after
verification. Keeping them separate means we can tighten the prompt contract
without changing the public output schema, and vice versa.

`llm_json_schema()` feeds the provider's structured-output mode.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LlmService(Base):
    name: str = Field(description="Short service name, e.g. 'Oral exams'")
    category: str | None = Field(
        default=None,
        description="preventive|diagnostic|restorative|endodontic|periodontic|oral_surgery|vision|other",
    )
    limit_count: float | None = Field(default=None, description="Numeric frequency limit, null if none stated")
    limit_unit: str | None = None
    limit_period: str | None = Field(default=None, description="per_year|per_calendar_year|per_month|per_lifetime")
    limit_raw: str | None = Field(default=None, description="The limit exactly as written")
    codes: list[str] = Field(default_factory=list, description="CDT codes listed under this service")
    evidence_quote: str = Field(description="VERBATIM text from the document supporting this service")


class LlmCostShare(Base):
    type: str = Field(description="copay|coinsurance|deductible|not_covered|unknown")
    amount_usd: float | None = None
    percent: float | None = None
    applies_to: str = Field(description="Which services this cost share applies to, as stated")
    evidence_quote: str


class LlmExclusion(Base):
    text: str = Field(description="One-line normalized statement of the exclusion")
    kind: str | None = Field(
        default=None,
        description="service_excluded|network_restriction|member_liability|claims_process|accumulator|other",
    )
    evidence_quote: str


class LlmPackageExtraction(Base):
    """What one package block must yield. Scalars the deterministic scanners
    already own (premium, benefit maximum, code descriptions) are absent on
    purpose: the model never re-types a number we can read ourselves."""

    package_name: str | None = Field(default=None, description="Full package heading as written, or null")
    package_name_evidence: str | None = None
    benefit_domains: list[str] = Field(default_factory=list, description="dental|vision|hearing|otc|other")
    network_restriction: str | None = Field(default=None, description="Network limitation, or null if not stated")
    network_restriction_evidence: str | None = None
    services: list[LlmService] = Field(default_factory=list)
    cost_share: list[LlmCostShare] = Field(default_factory=list)
    exclusions: list[LlmExclusion] = Field(default_factory=list)
    truncated: bool = Field(default=False, description="True if this block is cut off mid-statement")


def llm_json_schema() -> dict:
    return LlmPackageExtraction.model_json_schema()
