"""Docling as the fallback table reader.

Where it sits: last in `TableCascade`, asked for a page only when every
deterministic strategy scored zero — an exotic layout, or a scanned page with no
text layer. That keeps a heavyweight ML reader off the hot path, where the
geometric and ruled strategies are faster, reproducible and free.

Why Docling rather than a hosted document-AI service: it runs in-process, so no
page of a member's plan document leaves the boundary, there is no per-page bill,
and the same container produces the same output. The cost is a large dependency
and seconds-per-page latency, which is why it is optional and off by default.

Docling is imported lazily and converts a document once, caching per page: it
works on whole documents, so converting per page would repeat the expensive part
for every page of a scan.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.adapters.pdf.pdfplumber_source import PageView
from app.application.tables.markdown import parse_markdown_tables
from app.application.tables.models import (
    CODE_RE, Column, PageTable, RawRow, TableSchema, TableSegment, find_code)
from app.config import Settings
from app.domain.errors import ExtractionError, LLMPermanentError
from app.logging_setup import get_logger

log = get_logger("extract.docling")


class DoclingUnavailable(LLMPermanentError):
    code = "docling_unavailable"


class DoclingTableStrategy:
    """Implements the same `extract(page, carried)` contract as the deterministic
    strategies, so the cascade cannot tell them apart except by score."""

    name = "docling"

    def __init__(self, settings: Settings, converter: Any | None = None):
        self._s = settings
        self._converter = converter
        self._cache: dict[str, dict[int, list[TableSegment]]] = {}
        self._pages_used = 0
        if converter is None:
            self._check_available()

    # -- availability -------------------------------------------------------
    @staticmethod
    def _check_available() -> None:
        try:
            import docling  # noqa: F401
        except ImportError as exc:                       # optional heavyweight dependency
            raise DoclingUnavailable(
                "docling is not installed; `pip install docling` or set "
                "EXTRACT_DOCUMENT_AI_PROVIDER=none", stage="config") from exc

    def _build_converter(self) -> Any:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        options = PdfPipelineOptions()
        options.do_ocr = self._s.docling_ocr                  # only worth it for scans
        options.do_table_structure = True
        options.table_structure_options.do_cell_matching = True
        try:
            from docling.datamodel.pipeline_options import TableFormerMode

            options.table_structure_options.mode = (
                TableFormerMode.ACCURATE if self._s.docling_table_mode == "accurate"
                else TableFormerMode.FAST)
        except ImportError:                                   # older docling: mode not configurable
            pass
        if self._s.docling_artifacts_path:                    # pre-downloaded models, offline use
            options.artifacts_path = self._s.docling_artifacts_path
        return DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})

    def reset_budget(self) -> None:
        """Called per document. Resets the page ceiling and drops the cached
        conversion: a document is read page by page in one pass, so keeping its
        parsed tables afterwards would grow without bound in a long-running
        service."""
        self._pages_used = 0
        self._cache.clear()

    # -- strategy -----------------------------------------------------------
    async def extract(self, page: PageView, carried: TableSchema | None = None) -> PageTable:
        if not page.source_path:
            return PageTable(schema=carried, strategy=self.name)
        if self._pages_used >= self._s.document_ai_max_pages:
            log.warning("docling page budget exhausted", extra={
                "page": page.number, "limit": self._s.document_ai_max_pages})
            return PageTable(schema=carried, strategy=self.name)

        try:
            by_page = await self._convert(Path(page.source_path))
        except ExtractionError:
            raise
        except Exception as exc:                          # a model failure must not kill the run
            raise DoclingUnavailable(f"docling conversion failed: {exc}",
                                     stage="document_ai") from exc

        self._pages_used += 1
        segments = [self._verify(segment, page) for segment in by_page.get(page.number, [])]
        segments = [s for s in segments if s.rows]
        rows = [r for s in segments for r in s.rows]
        schema = next((s.schema for s in reversed(segments) if s.schema), carried)
        log.info("docling read page", extra={"page": page.number, "rows": len(rows),
                                             "segments": len(segments)})
        return PageTable(schema=schema, rows=rows, strategy=self.name,
                         segments=segments or [TableSegment(schema, [])])

    async def _convert(self, path: Path) -> dict[int, list[TableSegment]]:
        """Convert once per document, cached. Docling works on whole documents,
        so per-page conversion would repeat the expensive part every time."""
        key = str(path)
        if key in self._cache:
            return self._cache[key]
        if self._converter is None:
            self._converter = self._build_converter()
        log.info("docling converting document", extra={"file": path.name})
        # Docling is synchronous and CPU-bound; keep the event loop free.
        result = await asyncio.to_thread(self._converter.convert, str(path))
        self._cache[key] = self._segments_of(result)
        return self._cache[key]

    # -- docling document -> our table shape --------------------------------
    def _segments_of(self, result: Any) -> dict[int, list[TableSegment]]:
        document = getattr(result, "document", None)
        if document is None:
            return {}
        by_page: dict[int, list[TableSegment]] = {}
        for table in getattr(document, "tables", []) or []:
            page_no = self._page_of(table)
            segment = self._segment_of(table, page_no)
            if segment and segment.rows:
                by_page.setdefault(page_no, []).append(segment)
        if by_page:
            return by_page
        return self._from_markdown(document)

    @staticmethod
    def _page_of(table: Any) -> int:
        for prov in getattr(table, "prov", []) or []:
            page_no = getattr(prov, "page_no", None)
            if isinstance(page_no, int):
                return page_no
        return 1

    def _segment_of(self, table: Any, page_no: int) -> TableSegment | None:
        grid = self._grid_of(table)
        if not grid:
            return None
        header, *body = grid
        schema = TableSchema(
            columns=[Column(label=str(label), left=i, right=i + 1) for i, label in enumerate(header)],
            source=self.name)
        rows: list[RawRow] = []
        group: str | None = None
        for cells in body:
            cells = [str(c or "").strip() for c in cells]
            if not any(cells):
                continue
            if cells[0] and not any(cells[1:]):           # section heading row
                group = cells[0]
                continue
            if find_code(cells[0]) or any(find_code(c) for c in cells):
                rows.append(RawRow(cells=cells, group=group, page=page_no, y=float(len(rows))))
        schema.group = group
        return TableSegment(schema=schema, rows=rows)

    @staticmethod
    def _grid_of(table: Any) -> list[list[str]]:
        """Docling exposes tables as a dataframe; older versions expose a cell
        grid. Accept either, and give up quietly rather than raising."""
        try:
            frame = table.export_to_dataframe()
            return [[str(c) for c in frame.columns]] + \
                   [[str(v) for v in row] for row in frame.itertuples(index=False)]
        except Exception:
            pass
        try:
            grid = table.data.grid
            return [[str(getattr(cell, "text", "") or "") for cell in row] for row in grid]
        except Exception:
            return []

    @staticmethod
    def _from_markdown(document: Any) -> dict[int, list[TableSegment]]:
        """Last resort: the markdown export, parsed with the same parser used for
        any other markdown-producing reader."""
        try:
            markdown = document.export_to_markdown()
        except Exception:
            return {}
        segments = parse_markdown_tables(markdown, page_number=1)
        return {1: segments} if segments else {}

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
                log.warning("docling row dropped: code not on the page", extra={
                    "page": page.number, "code": code})
                continue
            kept.append(row)
        return TableSegment(schema=segment.schema, rows=kept)
