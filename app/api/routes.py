"""Two endpoints, plus a liveness probe."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from fastapi.responses import PlainTextResponse

from app.api.deps import get_service
from app.api.schemas import ErrorResponse, ExtractRequest, JobResponse
from app.application.csv_export import to_csv_string
from app.application.service import ExtractionService
from app.domain.errors import UnsupportedProjection
from app.domain.models import DentalGuideResult, SCHEMA_VERSION

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


@router.post("/extract/upload", response_model=JobResponse, responses=ERRORS,
             status_code=status.HTTP_202_ACCEPTED, summary="Submit a PDF (Dental Guide) for extraction")
async def submit_pdf(response: Response, file: UploadFile = File(...),
                     service: ExtractionService = Depends(get_service)) -> JobResponse:
    """Same job lifecycle as POST /extract; read the result back from
    GET /extract/{job_id}, or GET /extract/{job_id}?format=csv for the CSV."""
    job, created = await service.submit_pdf(await file.read(), filename=file.filename or "upload.pdf")
    response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    return JobResponse.from_job(job, replay=not created)


@router.get("/extract/{job_id}", responses=ERRORS, summary="Fetch job status and result")
async def get_extraction(job_id: str, format: str = Query("json", pattern="^(json|csv)$"),
                         service: ExtractionService = Depends(get_service)):
    """`format=csv` projects a Dental Guide result onto the customer's CSV
    columns; values the guide does not state are written as '-'."""
    job = await service.get(job_id)
    if format == "json":
        return JobResponse.from_job(job)
    payload = job.payload
    if not isinstance(payload, DentalGuideResult):
        raise UnsupportedProjection(
            "this job has no CSV projection; it extracted packages, not benefit rows",
            stage="serialize", details={"job_kind": job.kind.value})
    return PlainTextResponse(
        to_csv_string(payload.rows), media_type="text/csv",
        headers={"content-disposition": f'attachment; filename="{job.job_id}.csv"'})


@router.get("/health", summary="Liveness and version")
async def health() -> dict:
    return {"status": "ok", "schema_version": SCHEMA_VERSION}
