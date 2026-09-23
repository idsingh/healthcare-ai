"""HTTP DTOs. Kept separate from the domain model so the wire format and the
domain can evolve independently."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.models import DentalGuideResult, ExtractionResult, Job, JobError, JobKind, JobStatus


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=40, max_length=2_000_000,
                      description="Plain text extracted from the source document")
    document_id: str | None = Field(default=None, max_length=128,
                                    description="Caller's own identifier, echoed in logs")

    @field_validator("text")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be blank")
        return v


class Links(BaseModel):
    self: str
    csv: str | None = None


class JobResponse(BaseModel):
    job_id: str
    kind: JobKind
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    content_sha256: str
    idempotent_replay: bool = False
    needs_review: bool | None = None
    error: JobError | None = None
    result: ExtractionResult | DentalGuideResult | None = None
    row_count: int | None = None
    links: Links

    @classmethod
    def from_job(cls, job: Job, *, replay: bool = False) -> "JobResponse":
        payload = job.payload
        csv_link = (f"/extract/{job.job_id}?format=csv"
                    if job.kind is JobKind.dental_guide and payload else None)
        return cls(
            job_id=job.job_id, kind=job.kind, status=job.status, created_at=job.created_at,
            updated_at=job.updated_at, content_sha256=job.content_sha256,
            idempotent_replay=replay,
            needs_review=payload.validation.needs_review if payload else None,
            error=job.error, result=payload,
            row_count=len(payload.rows) if isinstance(payload, DentalGuideResult) else None,
            links=Links(self=f"/extract/{job.job_id}", csv=csv_link))


class ErrorBody(BaseModel):
    code: str
    message: str
    stage: str | None = None
    details: dict = {}


class ErrorResponse(BaseModel):
    error: ErrorBody
