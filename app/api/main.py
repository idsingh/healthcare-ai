"""HTTP entrypoint: middleware, error translation, lifespan.

The web layer is thin by design — it validates input, calls the application
service and maps domain errors onto status codes. No business logic here.
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.deps import get_service
from app.api.routes import router
from app.config import PIPELINE_VERSION, get_settings
from app.domain.errors import ExtractionError
from app.logging_setup import configure_logging, get_logger, set_request_id

log = get_logger("extract.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.json_logs)
    log.info("service starting", extra={"version": PIPELINE_VERSION,
                                        "llm_provider": settings.llm_provider,
                                        "model_id": settings.model_id})
    yield
    await get_service().drain()
    log.info("service stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="EOC Benefit Extraction Service",
        version=PIPELINE_VERSION,
        description="Turns plan document text into schema-conformant, evidence-anchored JSON.",
        lifespan=lifespan,
    )
    app.include_router(router)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        set_request_id(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            set_request_id(None)
        response.headers["x-request-id"] = request_id
        log.info("request", extra={"method": request.method, "path": request.url.path,
                                   "status": response.status_code, "request_id": request_id,
                                   "duration_ms": round((time.perf_counter() - started) * 1000)})
        return response

    @app.exception_handler(ExtractionError)
    async def domain_error(request: Request, exc: ExtractionError):
        log.warning("request rejected", extra={"error_code": exc.code, "stage": exc.stage})
        return JSONResponse(status_code=exc.http_status, content={"error": {
            "code": exc.code, "message": exc.message, "stage": exc.stage, "details": exc.details}})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"error": {
            "code": "invalid_request", "message": "request body failed validation",
            "stage": "input_validation", "details": {"errors": exc.errors()[:10]}}})

    return app


app = create_app()
