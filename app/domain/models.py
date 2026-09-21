"""Domain model = the output contract. Framework-free, Pydantic only.

Every extracted value is wrapped in Field[T] so evidence, status, confidence
and provenance are uniform across the whole document (DRY): one wrapper, one
set of validation rules, one thing for consumers to learn.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field as PField

T = TypeVar("T")

SCHEMA_VERSION = "1.0.0"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FieldStatus(str, Enum):
    found = "found"
    not_stated = "not_stated"
    ambiguous = "ambiguous"
    unverified = "unverified"   # evidence failed groundedness; value withheld
    truncated = "truncated"     # cut off by the document boundary


class Source(str, Enum):
    deterministic = "deterministic"
    llm = "llm"
    merged = "merged"


class Cadence(str, Enum):
    monthly = "monthly"
    quarterly = "quarterly"
    annual = "annual"
    per_visit = "per_visit"
    one_time = "one_time"


class Period(str, Enum):
    per_year = "per_year"
    per_calendar_year = "per_calendar_year"
    per_month = "per_month"
    per_lifetime = "per_lifetime"


class Severity(str, Enum):
    error = "error"
    warn = "warn"
    info = "info"


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Base):
    quote: str = PField(min_length=1)
    start: int | None = None
    end: int | None = None
    page: int | None = None


class Field(Base, Generic[T]):
    value: T | None = None
    status: FieldStatus = FieldStatus.not_stated
    confidence: float | None = PField(default=None, ge=0, le=1)
    source: Source | None = None
    evidence: Evidence | None = None
    notes: str | None = None

    @classmethod
    def missing(cls, notes: str | None = None) -> "Field[T]":
        return cls(value=None, status=FieldStatus.not_stated, notes=notes)


class Premium(Base):
    amount_usd: float = PField(ge=0)
    cadence: Cadence | None = None
    currency: str = "USD"


class BenefitMaximum(Base):
    amount_usd: float = PField(ge=0)
    period: Period | None = None
    applies_to: str | None = None
    currency: str = "USD"


class Limit(Base):
    count: float | None = None
    unit: str | None = None
    period: Period | None = None
    raw: str | None = None


class Code(Base):
    code: str = PField(pattern=r"^D\d{4}$")
    code_system: str = "CDT"
    description: str | None = None
    evidence: Evidence | None = None


class ServiceCategory(str, Enum):
    preventive = "preventive"
    diagnostic = "diagnostic"
    restorative = "restorative"
    endodontic = "endodontic"
    periodontic = "periodontic"
    oral_surgery = "oral_surgery"
    vision = "vision"
    other = "other"


class Service(Base):
    service_id: str
    name: str
    category: ServiceCategory | None = None
    limit: Limit | None = None
    codes: list[str] = []
    status: FieldStatus = FieldStatus.found
    confidence: float | None = None
    source: Source | None = None
    evidence: Evidence | None = None
    notes: str | None = None


class CostShareType(str, Enum):
    copay = "copay"
    coinsurance = "coinsurance"
    deductible = "deductible"
    not_covered = "not_covered"
    unknown = "unknown"


class AppliesTo(Base):
    description: str | None = None
    service_ids: list[str] = []
    codes: list[str] = []


class CostShare(Base):
    cost_share_id: str
    type: CostShareType
    amount_usd: float | None = None
    percent: float | None = PField(default=None, ge=0, le=100)
    applies_to: AppliesTo = AppliesTo()
    status: FieldStatus = FieldStatus.found
    confidence: float | None = None
    source: Source | None = None
    evidence: Evidence | None = None
    notes: str | None = None


class ExclusionKind(str, Enum):
    service_excluded = "service_excluded"
    network_restriction = "network_restriction"
    member_liability = "member_liability"
    claims_process = "claims_process"
    accumulator = "accumulator"
    other = "other"


class Exclusion(Base):
    exclusion_id: str
    text: str
    kind: ExclusionKind | None = None
    status: FieldStatus = FieldStatus.found
    confidence: float | None = None
    source: Source | None = None
    evidence: Evidence | None = None


class SourceSpan(Base):
    start: int | None = None
    end: int | None = None
    pages: list[int] = []


class Package(Base):
    package_id: str
    ordinal: int | None = None
    name: Field[str] = Field[str].missing()
    benefit_domains: list[str] = []
    premium: Field[Premium] = Field[Premium].missing()
    benefit_maximum: Field[BenefitMaximum] = Field[BenefitMaximum].missing()
    network_restriction: Field[str] = Field[str].missing()
    services: list[Service] = []
    codes: list[Code] = []
    cost_share: list[CostShare] = []
    exclusions: list[Exclusion] = []
    source_span: SourceSpan = SourceSpan()
    needs_review: bool = False
    degraded: bool = False   # LLM pass failed; deterministic-only content


class DocumentInfo(Base):
    plan_name: Field[str] = Field[str].missing()
    plan_year: Field[int] = Field[int].missing()
    issuer: Field[str] = Field[str].missing()
    document_type: Field[str] = Field[str].missing()
    form_id: Field[str] = Field[str].missing()
    source_pages: list[int] = []
    truncated: bool = False
    char_count_normalized: int | None = None


class Flag(Base):
    rule: str
    severity: Severity
    message: str
    path: str | None = None


class ValidationMetrics(Base):
    fields_total: int = 0
    fields_grounded: int = 0
    fields_null: int = 0
    groundedness_rate: float = 0.0


class ValidationReport(Base):
    passed: bool = True
    needs_review: bool = False
    flags: list[Flag] = []
    metrics: ValidationMetrics = ValidationMetrics()


class RunInfo(Base):
    run_id: str
    content_sha256: str
    idempotency_key: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    model_id: str | None = None
    model_params: dict = {}
    prompt_version: str | None = None
    pipeline_version: str | None = None
    degraded: bool = False
    partial_reason: str | None = None
    blocks_total: int = 0
    blocks_degraded: int = 0
    llm_calls: int = 0
    llm_retries: int = 0


class ExtractionResult(Base):
    schema_version: str = SCHEMA_VERSION
    run: RunInfo
    document: DocumentInfo = DocumentInfo()
    packages: list[Package] = []
    validation: ValidationReport = ValidationReport()


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    partial = "partial"      # result produced, but degraded or review-flagged
    failed = "failed"


class JobError(Base):
    code: str
    message: str
    stage: str | None = None
    retryable: bool = False
    details: dict = {}


class Job(Base):
    job_id: str
    status: JobStatus = JobStatus.queued
    content_sha256: str
    idempotency_key: str
    created_at: datetime = PField(default_factory=utcnow)
    updated_at: datetime = PField(default_factory=utcnow)
    result: ExtractionResult | None = None
    error: JobError | None = None

    def touch(self, status: JobStatus) -> "Job":
        return self.model_copy(update={"status": status, "updated_at": utcnow()})
