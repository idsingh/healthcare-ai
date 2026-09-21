"""Test double: replays a queued script of responses or exceptions.

Lets tests drive every branch the real provider can produce — transient
failure then success, malformed JSON, contract violation, hallucinated
evidence — without a network call.
"""
from __future__ import annotations

from typing import Any, Iterable

from app.domain.errors import LLMOutputError


class ScriptedLLMClient:
    model_id = "scripted/test"

    def __init__(self, script: Iterable[Any]):
        self._script = list(script)
        self.calls: list[dict] = []

    async def complete_json(self, *, system: str, user: str, json_schema: dict,
                            schema_name: str, seed: int | None = None,
                            temperature: float = 0.0) -> dict:
        self.calls.append({"system": system, "user": user, "seed": seed, "temperature": temperature})
        if not self._script:
            raise AssertionError("ScriptedLLMClient ran out of scripted responses")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(user)
        if isinstance(item, str):
            raise LLMOutputError("response was not valid JSON", stage="llm_extract",
                                 details={"payload": item[:500]})
        return item
