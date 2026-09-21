"""OpenRouter adapter.

The only place that knows about HTTP, API keys or provider error codes.
Provider exceptions are translated into domain errors at this boundary so the
core can decide what is retryable without importing httpx.
"""
from __future__ import annotations

import json

import httpx

from app.config import Settings
from app.domain.errors import LLMOutputError, LLMPermanentError, LLMTransientError
from app.logging_setup import get_logger

log = get_logger("extract.openrouter")
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524}


class OpenRouterClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        if not settings.openrouter_api_key:
            raise LLMPermanentError("EXTRACT_OPENROUTER_API_KEY is not set", stage="config")
        self.model_id = settings.model_id
        self._s = settings
        self._auth = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
        self._client = client or httpx.AsyncClient(
            base_url=settings.openrouter_base_url,
            timeout=httpx.Timeout(settings.llm_timeout_seconds),
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}",
                     "Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete_json(self, *, system: str, user: str, json_schema: dict,
                            schema_name: str, seed: int | None = None,
                            temperature: float = 0.0) -> dict:
        body = {
            "model": self.model_id,
            "temperature": temperature,
            "top_p": 1,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # Structured output: the provider enforces the schema server-side.
            # If a model does not support it, the reply still passes through
            # Pydantic validation and the repair loop upstream.
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
            },
        }
        if seed is not None:
            body["seed"] = seed

        try:
            response = await self._client.post("/chat/completions", json=body, headers=self._auth)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise LLMTransientError(f"transport failure: {exc}", stage="llm_extract") from exc

        if response.status_code in TRANSIENT_STATUS:
            raise LLMTransientError(
                f"provider returned {response.status_code}", stage="llm_extract",
                details={"status": response.status_code, "body": response.text[:300]})
        if response.status_code >= 400:
            raise LLMPermanentError(
                f"provider rejected the request ({response.status_code})", stage="llm_extract",
                details={"status": response.status_code, "body": response.text[:300]})

        payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMOutputError("provider response had no message content", stage="llm_extract",
                                 details={"payload": str(payload)[:500]}) from exc
        if usage := payload.get("usage"):
            log.info("llm usage", extra={"model": self.model_id, **{
                k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}})
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMOutputError(f"model returned non-JSON content: {exc}", stage="llm_extract",
                                 details={"payload": content[:1000]}) from exc
        if not isinstance(parsed, dict):
            raise LLMOutputError("model returned JSON that is not an object", stage="llm_extract",
                                 details={"payload": content[:500]})
        return parsed
