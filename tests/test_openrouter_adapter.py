"""The provider adapter: HTTP reality mapped onto domain errors.

This is the boundary that decides what gets retried, so it is tested against a
mock transport rather than trusted.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.llm.openrouter import OpenRouterClient
from app.config import Settings
from app.domain.errors import LLMOutputError, LLMPermanentError, LLMTransientError


def build(handler) -> OpenRouterClient:
    settings = Settings(llm_provider="openrouter", openrouter_api_key="test-key",
                        model_id="openai/gpt-5.6-luna")
    return OpenRouterClient(settings, httpx.AsyncClient(
        base_url=settings.openrouter_base_url, transport=httpx.MockTransport(handler)))


def reply(content: str, usage: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                     "usage": usage or {"total_tokens": 10}})


async def call(client: OpenRouterClient):
    return await client.complete_json(system="s", user="u", json_schema={"type": "object"},
                                      schema_name="Test", seed=7)


async def test_happy_path_sends_structured_output_and_parses_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["auth"] = request.headers["authorization"]
        return reply('{"package_name": "Package 1"}')

    assert await call(build(handler)) == {"package_name": "Package 1"}
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["strict"] is True
    assert (seen["temperature"], seen["top_p"], seen["seed"]) == (0.0, 1, 7)
    assert seen["auth"] == "Bearer test-key"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_transient_statuses_are_retryable(status):
    client = build(lambda request: httpx.Response(status, text="busy"))
    with pytest.raises(LLMTransientError) as exc:
        await call(client)
    assert exc.value.retryable is True


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_client_errors_are_permanent(status):
    client = build(lambda request: httpx.Response(status, text="nope"))
    with pytest.raises(LLMPermanentError) as exc:
        await call(client)
    assert exc.value.retryable is False


async def test_timeout_is_transient():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMTransientError):
        await call(build(handler))


async def test_non_json_content_is_an_output_error():
    with pytest.raises(LLMOutputError):
        await call(build(lambda request: reply("Here you go: {broken")))


async def test_missing_message_content_is_an_output_error():
    client = build(lambda request: httpx.Response(200, json={"choices": []}))
    with pytest.raises(LLMOutputError):
        await call(client)


async def test_missing_api_key_fails_fast():
    with pytest.raises(LLMPermanentError):
        OpenRouterClient(Settings(llm_provider="openrouter", openrouter_api_key=None))
