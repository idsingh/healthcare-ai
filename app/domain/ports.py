"""Ports. The application core depends on these Protocols and nothing else;
adapters (OpenRouter, in-memory store) are injected at the composition root.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.models import Job


@runtime_checkable
class LLMClient(Protocol):
    """One method, because that is all the core needs (ISP).

    Implementations must raise LLMTransientError / LLMPermanentError /
    LLMOutputError from app.domain.errors — never provider-specific exceptions.
    Provider details stop at the adapter boundary.
    """

    model_id: str

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict,
        schema_name: str,
        seed: int | None = None,
        temperature: float = 0.0,
    ) -> dict:
        """Return parsed JSON. Raise LLMOutputError if the payload is not JSON."""
        ...


@runtime_checkable
class JobRepository(Protocol):
    async def create(self, job: Job) -> Job: ...

    async def get(self, job_id: str) -> Job | None: ...

    async def update(self, job: Job) -> Job: ...

    async def find_by_idempotency_key(self, key: str) -> Job | None: ...
