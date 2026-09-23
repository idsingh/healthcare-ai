"""Mistral Document AI as a table strategy.

Where it sits: last in `TableCascade`. It is asked for a page only when every
deterministic reader scored zero on that page — an exotic layout, or a scanned
page with no text layer at all. That keeps it off the hot path, where it would
cost money per page and give up determinism for nothing.

What leaves the process: one page of the document, as PDF bytes. Everything the
service returns is re-verified locally — codes must match the CDT shape and,
when the page has a text layer, must actually appear on that page — so a
hallucinated row cannot reach the CSV.

The request/response shape is confined to this module. If the vendor's wire
format differs from what is coded here, this is the only file that changes.
"""
from __future__ import annotations

import base64
import io

import httpx

from app.adapters.pdf.pdfplumber_source import PageView
from app.application.retry import retry_async
from app.application.tables.markdown import parse_markdown_tables
from app.application.tables.models import CODE_RE, PageTable, TableSchema, TableSegment
from app.config import Settings
from app.domain.errors import LLMOutputError, LLMPermanentError, LLMTransientError
from app.logging_setup import get_logger

log = get_logger("extract.document_ai")
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524}


class MistralDocumentAIStrategy:
    """Implements the same `extract(page, carried)` contract as the deterministic
    strategies, so the cascade cannot tell them apart except by score."""

    name = "document_ai"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        if not settings.mistral_api_key:
            raise LLMPermanentError("EXTRACT_MISTRAL_API_KEY is not set", stage="config")
        self._s = settings
        self._pages_used = 0
        self._client = client or httpx.AsyncClient(
            base_url=settings.mistral_base_url,
            timeout=httpx.Timeout(settings.document_ai_timeout_seconds))
        self._auth = {"Authorization": f"Bearer {settings.mistral_api_key}"}

    async def aclose(self) -> None:
        await self._client.aclose()

    def reset_budget(self) -> None:
        """Called per document: the page ceiling is a per-document cost guard."""
        self._pages_used = 0

    async def extract(self, page: PageView, carried: TableSchema | None = None) -> PageTable:
        if self._pages_used >= self._s.document_ai_max_pages:
            log.warning("document ai page budget exhausted", extra={
                "page": page.number, "limit": self._s.document_ai_max_pages})
            return PageTable(schema=carried, strategy=self.name)

        pdf_bytes = self._single_page_pdf(page)
        if pdf_bytes is None:
            return PageTable(schema=carried, strategy=self.name)

        self._pages_used += 1
        markdown = await self._ocr(pdf_bytes, page.number)
        segments = parse_markdown_tables(markdown, page_number=page.number,
                                         group=carried.group if carried else None)
        segments = [self._verify(segment, page) for segment in segments]
        segments = [s for s in segments if s.rows]
        rows = [r for s in segments for r in s.rows]
        schema = next((s.schema for s in reversed(segments) if s.schema), carried)
        log.info("document ai read page", extra={
            "page": page.number, "rows": len(rows), "segments": len(segments),
            "pages_used": self._pages_used})
        return PageTable(schema=schema, rows=rows, strategy=self.name,
                         segments=segments or [TableSegment(schema, [])])

    # -- verification -------------------------------------------------------
    def _verify(self, segment: TableSegment, page: PageView) -> TableSegment:
        """Nothing a model returns is trusted: the code must look like a dental
        code, and when the page has a text layer it must be on that page."""
        kept = []
        for row in segment.rows:
            code = row.code
            if not code or not CODE_RE.match(code):
                continue
            if page.has_text and code not in page.text:
                log.warning("document ai row dropped: code not on the page", extra={
                    "page": page.number, "code": code})
                continue
            kept.append(row)
        return TableSegment(schema=segment.schema, rows=kept)

    # -- transport ----------------------------------------------------------
    def _single_page_pdf(self, page: PageView) -> bytes | None:
        """Send one page, not the document: it bounds both cost and exposure."""
        if not page.source_path:
            return None
        try:
            from pypdf import PdfReader, PdfWriter

            reader = PdfReader(str(page.source_path))
            writer = PdfWriter()
            writer.add_page(reader.pages[page.number - 1])
            buffer = io.BytesIO()
            writer.write(buffer)
            return buffer.getvalue()
        except Exception as exc:                          # unreadable page: skip, do not crash
            log.warning("could not isolate page for document ai", extra={
                "page": page.number, "error": str(exc)[:200]})
            return None

    async def _ocr(self, pdf_bytes: bytes, page_number: int) -> str:
        payload = {
            "model": self._s.mistral_ocr_model,
            "document": {
                "type": "document_url",
                "document_url": "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode(),
            },
            "include_image_base64": False,
        }

        async def call() -> str:
            try:
                response = await self._client.post("/ocr", json=payload, headers=self._auth)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise LLMTransientError(f"document ai transport failure: {exc}",
                                        stage="document_ai") from exc
            if response.status_code in TRANSIENT_STATUS:
                raise LLMTransientError(f"document ai returned {response.status_code}",
                                        stage="document_ai",
                                        details={"status": response.status_code})
            if response.status_code >= 400:
                raise LLMPermanentError(f"document ai rejected the request "
                                        f"({response.status_code})", stage="document_ai",
                                        details={"body": response.text[:300]})
            return self._markdown_of(response.json())

        return await retry_async(
            call, attempts=self._s.max_llm_attempts,
            base_delay=self._s.retry_base_delay_seconds,
            max_delay=self._s.retry_max_delay_seconds,
            operation=f"document_ai.ocr(page={page_number})")

    @staticmethod
    def _markdown_of(payload: dict) -> str:
        """Defensive parsing: accept the documented shape and the obvious
        variants, fail loudly rather than returning something empty."""
        if not isinstance(payload, dict):
            raise LLMOutputError("document ai returned a non-object response", stage="document_ai")
        pages = payload.get("pages")
        if isinstance(pages, list) and pages:
            parts = [p.get("markdown") or p.get("text") or "" for p in pages if isinstance(p, dict)]
            joined = "\n".join(part for part in parts if part)
            if joined.strip():
                return joined
        for key in ("markdown", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        raise LLMOutputError("document ai response contained no page text", stage="document_ai",
                             details={"keys": list(payload)[:8]})
