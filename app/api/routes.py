"""Two endpoints, plus a liveness probe."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import get_service
from app.api.schemas import ErrorResponse, ExtractRequest, JobResponse
from app.application.service import ExtractionService
from app.domain.models import SCHEMA_VERSION

router = APIRouter()

ERRORS = {
    404: {"model": ErrorResponse, "description": "Unknown job"},
    422: {"model": ErrorResponse, "description": "Input rejected"},
}


@router.post("/extract", response_model=JobResponse, responses=ERRORS,
             status_code=status.HTTP_202_ACCEPTED, summary="Submit text for extraction")
async def submit_extraction(request: ExtractRequest, response: Response,
                            service: ExtractionService = Depends(get_service)) -> JobResponse:
    """Accepts the document and returns immediately with a job id.

    Submitting identical text under identical configuration returns the
    original job (200 instead of 202) rather than extracting twice.
    """
    job, created = await service.submit(request.text, document_id=request.document_id)
    response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    return JobResponse.from_job(job, replay=not created)


@router.get("/extract/{job_id}", response_model=JobResponse, responses=ERRORS,
            summary="Fetch job status and result")
async def get_extraction(job_id: str, service: ExtractionService = Depends(get_service)) -> JobResponse:
    return JobResponse.from_job(await service.get(job_id))


@router.get("/health", summary="Liveness and version")
async def health() -> dict:
    return {"status": "ok", "schema_version": SCHEMA_VERSION}
