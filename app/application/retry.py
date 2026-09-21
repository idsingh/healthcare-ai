"""Transient-failure retry: exponential backoff with full jitter.

Small on purpose. A retry policy is ~20 lines; pulling in a framework for it
would be infrastructure we do not need.
"""
from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

from app.domain.errors import ExtractionError
from app.logging_setup import get_logger

T = TypeVar("T")
log = get_logger("extract.retry")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float,
    max_delay: float,
    operation: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_attempt: Callable[[int], None] | None = None,
) -> T:
    """Retry only what declares itself retryable. Everything else fails fast."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        if on_attempt:
            on_attempt(attempt)
        try:
            return await fn()
        except ExtractionError as exc:
            last = exc
            if not exc.retryable or attempt == attempts:
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay = random.uniform(0, delay)          # full jitter
            log.warning("retrying after transient failure", extra={
                "operation": operation, "attempt": attempt, "max_attempts": attempts,
                "error_code": exc.code, "delay_seconds": round(delay, 3)})
            await sleep(delay)
    raise last  # pragma: no cover - unreachable
