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

    # Reliability
    max_llm_attempts: int = 3                        # transient retries per call
    max_repair_attempts: int = 1                     # schema-repair round trips
    retry_base_delay_seconds: float = 0.5
    retry_max_delay_seconds: float = 8.0
    block_concurrency: int = 4                       # parallel package blocks per doc
    job_concurrency: int = 8                         # parallel jobs in this process

    # Input validation
    max_input_bytes: int = 2_000_000
    min_input_chars: int = 40
    min_printable_ratio: float = 0.85

    # Ops
    log_level: str = "INFO"
    json_logs: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
