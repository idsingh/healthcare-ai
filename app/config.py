"""Settings. One object, injected; nothing reads os.environ directly."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

PIPELINE_VERSION = "1.0.0"
PROMPT_VERSION = "eoc-supplemental-v3"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EXTRACT_", env_file=".env", extra="ignore")

    # LLM
    llm_provider: str = "stub"                       # stub | openrouter
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_id: str = "openai/gpt-5.6-luna"
    temperature: float = 0.0
    seed: int = 7
    llm_timeout_seconds: float = 60.0

    # Fallback table reader, used only when deterministic readers fail on a page.
    # Docling runs locally: no page leaves the process and there is no per-page bill,
    # at the cost of a large dependency and seconds-per-page latency.
    document_ai_provider: str = "none"               # none | docling
    document_ai_mode: str = "fallback"               # fallback | always
    docling_ocr: bool = True                         # needed for scans; slower
    docling_table_mode: str = "accurate"             # accurate | fast
    docling_artifacts_path: str | None = None        # pre-downloaded models for offline use
    document_ai_timeout_seconds: float = 300.0
    document_ai_max_pages: int = 25                  # ceiling per document

    # Reliability
    max_llm_attempts: int = 3                        # transient retries per call
    max_repair_attempts: int = 1                     # schema-repair round trips
    retry_base_delay_seconds: float = 0.5
    retry_max_delay_seconds: float = 8.0
    block_concurrency: int = 4                       # parallel package blocks per doc
    dg_llm_grouping: bool = True                     # name benefit groups with the model
    job_concurrency: int = 8                         # parallel jobs in this process

    # Input validation
    max_input_bytes: int = 2_000_000
    max_pdf_bytes: int = 25_000_000
    min_input_chars: int = 40
    min_printable_ratio: float = 0.85

    # Ops
    log_level: str = "INFO"
    json_logs: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
