"""Job orchestration: submit, run in the background, read back.

Idempotency is the whole retry story at this level. The key is a hash of the
content plus everything that can change the output; re-submitting the same text
under the same configuration returns the original job instead of paying for a
second extraction.
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path

from app.application.dental_guide import DentalGuidePipeline
from app.application.pipeline import ExtractionPipeline
from app.config import PIPELINE_VERSION, PROMPT_VERSION, Settings
from app.domain.errors import ExtractionError, InputRejected, JobNotFound
from app.domain.models import Job, JobError, JobKind, JobStatus, utcnow
from app.domain.ports import JobRepository
from app.logging_setup import get_logger, set_job_id

log = get_logger("extract.service")


class ExtractionService:
    def __init__(self, repo: JobRepository, pipeline: ExtractionPipeline, settings: Settings,
                 dental_guide: DentalGuidePipeline | None = None):
        self._repo = repo
        self._pipeline = pipeline
        self._dental_guide = dental_guide
        self._s = settings
        self._slots = asyncio.Semaphore(settings.job_concurrency)
        self._tasks: set[asyncio.Task] = set()

    # -- public API ---------------------------------------------------------
    async def submit(self, text: str, *, document_id: str | None = None) -> tuple[Job, bool]:
        """Returns (job, created). created=False means an identical submission
        is already known and was replayed."""
        content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = self._idempotency_key(content_sha)

        if existing := await self._repo.find_by_idempotency_key(key):
            log.info("idempotent replay", extra={"job_id": existing.job_id, "status": existing.status})
            return existing, False

        job = Job(job_id=f"job_{uuid.uuid4().hex[:16]}", content_sha256=content_sha,
                  idempotency_key=key, status=JobStatus.queued)
        await self._repo.create(job)
        log.info("job accepted", extra={"job_id": job.job_id, "document_id": document_id,
                                        "content_sha256": content_sha, "input_chars": len(text)})
        self._spawn(job, text)
        return job, True

    async def submit_pdf(self, data: bytes, *, filename: str) -> tuple[Job, bool]:
        """Dental Guide PDFs take the same job lifecycle as text: validate, hash
        for idempotency, run in the background, read back by job id."""
        self._check_pdf(data, filename)
        content_sha = hashlib.sha256(data).hexdigest()
        key = self._idempotency_key(content_sha)

        if existing := await self._repo.find_by_idempotency_key(key):
            log.info("idempotent replay", extra={"job_id": existing.job_id, "status": existing.status})
            return existing, False

        job = Job(job_id=f"job_{uuid.uuid4().hex[:16]}", kind=JobKind.dental_guide,
                  content_sha256=content_sha, idempotency_key=key, status=JobStatus.queued)
        await self._repo.create(job)
        log.info("pdf job accepted", extra={"job_id": job.job_id, "file_name": filename,
                                            "bytes": len(data)})
        task = asyncio.create_task(self._run_pdf(job, data, filename), name=job.job_id)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job, True

    def _check_pdf(self, data: bytes, filename: str) -> None:
        if not data:
            raise InputRejected("uploaded file is empty", stage="input_validation")
        if len(data) > self._s.max_pdf_bytes:
            raise InputRejected(
                f"file is {len(data)} bytes, limit is {self._s.max_pdf_bytes}",
                stage="input_validation", details={"bytes": len(data)})
        if not data.startswith(b"%PDF"):
            raise InputRejected(
                f"{filename} is not a PDF (missing %PDF header)", stage="input_validation")

    async def _run_pdf(self, job: Job, data: bytes, filename: str) -> None:
        set_job_id(job.job_id)
        if self._dental_guide is None:
            await self._fail(job, ExtractionError("dental guide extraction is not configured",
                                                  stage="config"))
            return
        async with self._slots:
            await self._repo.update(job.touch(JobStatus.running))
            tmp = Path(tempfile.mkdtemp(prefix="dg_")) / Path(filename).name
            try:
                tmp.write_bytes(data)
                result = await self._dental_guide.run(tmp, run_id=job.job_id)
            except InputRejected as exc:
                await self._fail(job, exc, level="warning")
            except ExtractionError as exc:
                await self._fail(job, exc)
            except Exception as exc:
                log.exception("pdf job crashed", extra={"job_id": job.job_id})
                await self._repo.update(job.model_copy(update={
                    "status": JobStatus.failed, "updated_at": utcnow(),
                    "error": JobError(code="internal_error", message=str(exc), stage="dental_guide")}))
            else:
                status = JobStatus.succeeded if result.validation.passed else JobStatus.partial
                await self._repo.update(job.model_copy(update={
                    "status": status, "updated_at": utcnow(), "rows_result": result}))
                log.info("pdf job finished", extra={"job_id": job.job_id, "status": status,
                                                    "rows": len(result.rows)})
            finally:
                shutil.rmtree(tmp.parent, ignore_errors=True)
                set_job_id(None)

    async def get(self, job_id: str) -> Job:
        job = await self._repo.get(job_id)
        if job is None:
            raise JobNotFound(f"no job with id {job_id}")
        return job

    async def drain(self, timeout: float = 30.0) -> None:
        """Let in-flight jobs finish on shutdown; used by the app lifespan and
        by tests that need deterministic completion."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    # -- internals ----------------------------------------------------------
    def _idempotency_key(self, content_sha: str) -> str:
        parts = [content_sha, PIPELINE_VERSION, PROMPT_VERSION, self._s.model_id,
                 str(self._s.seed), str(self._s.temperature)]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def _spawn(self, job: Job, text: str) -> None:
        task = asyncio.create_task(self._run(job, text), name=job.job_id)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, job: Job, text: str) -> None:
        set_job_id(job.job_id)
        async with self._slots:
            await self._repo.update(job.touch(JobStatus.running))
            try:
                result = await self._pipeline.run(
                    text, run_id=job.job_id, idempotency_key=job.idempotency_key)
            except InputRejected as exc:
                await self._fail(job, exc, level="warning")
            except ExtractionError as exc:
                await self._fail(job, exc)
            except Exception as exc:                       # unexpected: still a useful state
                log.exception("job crashed", extra={"job_id": job.job_id})
                await self._repo.update(job.model_copy(update={
                    "status": JobStatus.failed, "updated_at": utcnow(),
                    "error": JobError(code="internal_error", message=str(exc), stage="pipeline")}))
            else:
                status = (JobStatus.partial
                          if result.run.degraded or not result.validation.passed
                          else JobStatus.succeeded)
                await self._repo.update(job.model_copy(update={
                    "status": status, "updated_at": utcnow(), "result": result}))
                log.info("job finished", extra={"job_id": job.job_id, "status": status})
            finally:
                set_job_id(None)

    async def _fail(self, job: Job, exc: ExtractionError, level: str = "error") -> None:
        getattr(log, level)("job failed", extra={
            "job_id": job.job_id, "error_code": exc.code, "stage": exc.stage, "reason": exc.message})
        await self._repo.update(job.model_copy(update={
            "status": JobStatus.failed, "updated_at": utcnow(),
            "error": JobError(code=exc.code, message=exc.message, stage=exc.stage,
                              retryable=exc.retryable, details=exc.details)}))
