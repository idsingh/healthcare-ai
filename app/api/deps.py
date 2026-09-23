"""Composition root: the one place adapters are chosen and injected.

Everything above this file depends on ports only, which is what makes the LLM
swappable (`EXTRACT_LLM_PROVIDER=stub|openrouter`) and mockable in tests
(`app.dependency_overrides[get_service] = ...`).
"""
from __future__ import annotations

from functools import lru_cache

from app.adapters.llm.openrouter import OpenRouterClient
from app.adapters.llm.stub import StubLLMClient
from app.adapters.repository.memory import InMemoryJobRepository
from app.application.dental_guide import DentalGuidePipeline
from app.application.tables.cascade import TableCascade
from app.application.pipeline import ExtractionPipeline
from app.application.service import ExtractionService
from app.config import Settings, get_settings
from app.domain.ports import LLMClient
from app.logging_setup import get_logger

log = get_logger("extract.deps")


def build_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "openrouter":
        log.info("using openrouter llm", extra={"model": settings.model_id})
        return OpenRouterClient(settings)
    log.warning("using offline stub llm; set EXTRACT_LLM_PROVIDER=openrouter for real extraction")
    return StubLLMClient()


def build_document_ai(settings: Settings):
    """Optional fallback reader for pages no deterministic strategy can parse.

    Off unless EXTRACT_DOCUMENT_AI_PROVIDER says otherwise: it is a heavyweight
    dependency with seconds-per-page latency, and the deterministic readers
    handle digital-text guides on their own. A missing or broken install
    degrades to deterministic-only rather than failing the service.
    """
    if settings.document_ai_provider not in ("docling",):
        if settings.document_ai_provider not in ("none", ""):
            log.warning("unknown document ai provider; deterministic readers only",
                        extra={"provider": settings.document_ai_provider})
        return None
    try:
        from app.adapters.document_ai.docling import DoclingTableStrategy

        strategy = DoclingTableStrategy(settings)
    except Exception as exc:
        # A broken install can raise anything on import (OSError from a bad
        # native wheel, RuntimeError from a version clash). Any of them must
        # degrade to deterministic-only rather than failing every request.
        log.warning("docling fallback unavailable; deterministic readers only",
                    extra={"reason": str(exc)[:200]})
        return None
    log.info("docling fallback enabled", extra={"ocr": settings.docling_ocr,
                                                "table_mode": settings.docling_table_mode})
    return strategy


def build_cascade(settings: Settings) -> TableCascade:
    fallback = build_document_ai(settings)
    if fallback and settings.document_ai_mode == "always":
        log.info("document ai runs first; deterministic readers are the backup")
    return TableCascade(fallbacks=[fallback] if fallback else None,
                        fallback_first=bool(fallback) and settings.document_ai_mode == "always")


def build_service(settings: Settings | None = None, llm: LLMClient | None = None) -> ExtractionService:
    settings = settings or get_settings()
    llm = llm or build_llm(settings)
    return ExtractionService(
        repo=InMemoryJobRepository(),
        pipeline=ExtractionPipeline(settings, llm),
        settings=settings,
        dental_guide=DentalGuidePipeline(settings, llm, cascade=build_cascade(settings)))


@lru_cache
def _singleton() -> ExtractionService:
    return build_service()


def get_service() -> ExtractionService:
    """FastAPI dependency. Overridden wholesale in tests."""
    return _singleton()
