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


def build_service(settings: Settings | None = None, llm: LLMClient | None = None) -> ExtractionService:
    settings = settings or get_settings()
    llm = llm or build_llm(settings)
    return ExtractionService(
        repo=InMemoryJobRepository(),
        pipeline=ExtractionPipeline(settings, llm),
        settings=settings,
        dental_guide=DentalGuidePipeline(settings, llm))


@lru_cache
def _singleton() -> ExtractionService:
    return build_service()


def get_service() -> ExtractionService:
    """FastAPI dependency. Overridden wholesale in tests."""
    return _singleton()
