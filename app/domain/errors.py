"""Domain errors. Each carries a stable `code` (contract with API clients) and
a `retryable` flag (contract with the retry helper)."""
from __future__ import annotations


class ExtractionError(Exception):
    code = "extraction_error"
    retryable = False
    http_status = 500

    def __init__(self, message: str, *, stage: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.details = details or {}


class InputRejected(ExtractionError):
    """Input failed validation before any work was done."""
    code = "input_rejected"
    http_status = 422


class JobNotFound(ExtractionError):
    code = "job_not_found"
    http_status = 404


class LLMTransientError(ExtractionError):
    """Timeout, 429, 5xx, connection reset — worth retrying as-is."""
    code = "llm_transient"
    retryable = True
    http_status = 503


class LLMPermanentError(ExtractionError):
    """Auth failure, unknown model, request rejected — retrying changes nothing."""
    code = "llm_permanent"
    http_status = 502


class LLMOutputError(ExtractionError):
    """The call succeeded but the payload is not usable: not JSON, or it does
    not satisfy the contract. Repairable by asking again with the error."""
    code = "llm_output_invalid"
    retryable = False
    http_status = 502


class BlockExtractionFailed(ExtractionError):
    """All attempts for one block are exhausted. The pipeline degrades this
    block to deterministic-only output rather than failing the document."""
    code = "block_extraction_failed"
