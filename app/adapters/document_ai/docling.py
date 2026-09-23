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
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.adapters.pdf.pdfplumber_source import PageView
from app.application.preprocess import collapse
from app.application.tables.markdown import parse_markdown_tables
from app.application.tables.models import (
    CODE_RE, Column, PageTable, RawRow, TableSchema, TableSegment, find_code)
from app.config import Settings
from app.domain.errors import LLMPermanentError
from app.logging_setup import get_logger

log = get_logger("extract.docling")
MAX_CACHED_DOCUMENTS = 2         # a job reads one document; a couple may overlap


class DoclingUnavailable(LLMPermanentError):
    code = "docling_unavailable"


class DoclingBudgetExhausted(DoclingUnavailable):
    """This document has used its page allowance; later pages are not read."""
    code = "docling_budget_exhausted"


WORD = re.compile(r"[a-z0-9]{3,}")
MIN_WORD_SUPPORT = 0.6      # share of a cell's words that must be on the page


def _tokens(text: str) -> set[str]:
    return set(WORD.findall(text.lower()))


def _supported_by(cell: str, page_tokens: set[str]) -> bool:
    """Is this cell's text actually on the page?

    Checked by word coverage, not by substring: a two-column table flattened
    into reading order splits a description across the page text ('Periodic oral
    evaluation - established' ... 'patient'), so a contiguous match would reject
    text that is genuinely there. Invented text still fails, because its words
    are not on the page at all.
    """
    words = _tokens(collapse(cell))
    if not words:
        return True
    present = sum(1 for w in words if w in page_tokens)
    return present / len(words) >= MIN_WORD_SUPPORT


@dataclass
class _Conversion:
    """One document's conversion: its tables, what it has served, and why it
    failed if it did. Keyed by path so concurrent jobs cannot collide."""
    by_page: dict[int, list[TableSegment]] = field(default_factory=dict)
    unassigned: list[TableSegment] = field(default_factory=list)
    pages_served: int = 0
    failed: str | None = None


class DoclingTableStrategy:
    """Implements the same `extract(page, carried)` contract as the deterministic
    strategies, so the cascade cannot tell them apart except by score."""

    name = "docling"

    def __init__(self, settings: Settings, converter: Any | None = None):
        self._s = settings
        self._converter = converter
        # State is per document and keyed by path, never per instance: one
        # strategy object is shared by every job in the process, so a second
        # document must not be able to clear or charge the first one's.
        self._documents: OrderedDict[str, _Conversion] = OrderedDict()
        self._lock = asyncio.Lock()
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
        """Per-document hook. Nothing to reset: budget and cache are keyed by
        document path, so documents cannot interfere with each other. Memory is
        bounded by evicting the least recently used conversion instead."""
        return None

    # -- strategy -----------------------------------------------------------
    async def extract(self, page: PageView, carried: TableSchema | None = None) -> PageTable:
        if not page.source_path:
            return PageTable(schema=carried, strategy=self.name)

        conversion = await self._conversion_for(Path(page.source_path))
        if conversion.failed:
            raise DoclingUnavailable(conversion.failed, stage="document_ai")
        if conversion.pages_served >= self._s.document_ai_max_pages:
            log.warning("docling page budget exhausted", extra={
                "page": page.number, "limit": self._s.document_ai_max_pages})
            raise DoclingBudgetExhausted(
                f"docling page budget of {self._s.document_ai_max_pages} reached for this document",
                stage="document_ai")
        conversion.pages_served += 1

        by_page = conversion.by_page
        found = by_page.get(page.number)
        if found is None and conversion.unassigned:
            # The markdown fallback loses page provenance. Serve those tables to
            # the first page that asks, rather than claiming they are on page 1.
            found = conversion.unassigned
            conversion.unassigned = []
            for segment in found:
                for row in segment.rows:
                    row.page = page.number
        segments = [self._verify(segment, page) for segment in (found or [])]
        segments = [s for s in segments if s.rows]
        rows = [r for s in segments for r in s.rows]
        schema = next((s.schema for s in reversed(segments) if s.schema), carried)
        log.info("docling read page", extra={"page": page.number, "rows": len(rows),
                                             "segments": len(segments)})
        return PageTable(schema=schema, rows=rows, strategy=self.name,
                         segments=segments or [TableSegment(schema, [])])

    async def _conversion_for(self, path: Path) -> "_Conversion":
        """Convert once per document, cached and bounded. Conversion is the
        expensive part — seconds per document — so per-page conversion would pay
        it again for every page. A failure is remembered too, or a broken model
        would be re-run for every page of the document."""
        key = str(path)
        async with self._lock:
            if key in self._documents:
                self._documents.move_to_end(key)
                return self._documents[key]
            conversion = _Conversion()
            self._documents[key] = conversion
            while len(self._documents) > MAX_CACHED_DOCUMENTS:
                self._documents.popitem(last=False)

        if self._converter is None:
            try:
                self._converter = self._build_converter()
            except Exception as exc:
                conversion.failed = f"docling could not be initialised: {exc}"
                return conversion

        log.info("docling converting document", extra={"file": path.name})
        try:
            # Docling is synchronous and CPU-bound: keep the event loop free, and
            # bound it, or one hung conversion blocks the job for ever.
            result = await asyncio.wait_for(
                asyncio.to_thread(self._converter.convert, str(path)),
                timeout=self._s.document_ai_timeout_seconds)
        except asyncio.TimeoutError:
            conversion.failed = (f"docling conversion exceeded "
                                 f"{self._s.document_ai_timeout_seconds:.0f}s")
            return conversion
        except Exception as exc:
            conversion.failed = f"docling conversion failed: {exc}"
            return conversion

        by_page, unassigned = self._segments_of(result)
        conversion.by_page, conversion.unassigned = by_page, unassigned
        return conversion

    # -- docling document -> our table shape --------------------------------
    def _segments_of(self, result: Any) -> tuple[dict[int, list[TableSegment]], list[TableSegment]]:
        """Returns (tables with a known page, tables whose page is unknown)."""
        document = getattr(result, "document", None)
        if document is None:
            return {}, []
        by_page: dict[int, list[TableSegment]] = {}
        for table in getattr(document, "tables", []) or []:
            page_no = self._page_of(table)
            segment = self._segment_of(table, page_no)
            if segment and segment.rows:
                by_page.setdefault(page_no, []).append(segment)
        if by_page:
            return by_page, []
        return {}, self._from_markdown(document)

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
        """Docling's table export has changed across versions: accept the
        dataframe export, else the cell grid. Only a *shape* mismatch is treated
        as 'try the next form' — anything else is logged, so a real failure is
        not indistinguishable from an old docling."""
        try:
            frame = table.export_to_dataframe()
            return [[str(c) for c in frame.columns]] + \
                   [[str(v) for v in row] for row in frame.itertuples(index=False)]
        except (AttributeError, ImportError, TypeError):
            pass
        except Exception as exc:
            log.warning("docling dataframe export failed", extra={"error": str(exc)[:200]})
        try:
            grid = table.data.grid
            return [[str(getattr(cell, "text", "") or "") for cell in row] for row in grid]
        except (AttributeError, TypeError):
            return []
        except Exception as exc:
            log.warning("docling cell grid unreadable", extra={"error": str(exc)[:200]})
            return []

    @staticmethod
    def _from_markdown(document: Any) -> list[TableSegment]:
        """Last resort: the markdown export, parsed with the same parser used for
        any other markdown-producing reader. Markdown carries no page numbers, so
        these are returned as unassigned rather than claimed to be on page 1."""
        try:
            markdown = document.export_to_markdown()
        except Exception as exc:
            log.warning("docling markdown export failed", extra={"error": str(exc)[:200]})
            return []
        return parse_markdown_tables(markdown, page_number=0)

    # -- verification -------------------------------------------------------
    def _verify(self, segment: TableSegment, page: PageView) -> TableSegment:
        """Nothing a model returns is trusted. The code must look like a dental
        code and, when the page has a text layer, be on that page. Other cells
        are checked against the page too and blanked when they are not there, so
        a plausible-looking invented frequency or percentage cannot reach the
        CSV. A page with no text layer has nothing to check against; those rows
        are flagged downstream instead."""
        page_text = collapse(page.text) if page.has_text else ""
        page_tokens = _tokens(page_text)
        kept = []
        for row in segment.rows:
            code = row.code
            if not code or not CODE_RE.match(code):
                continue
            if page_text and code not in page_text:
                log.warning("docling row dropped: code not on the page", extra={
                    "page": page.number, "code": code})
                continue
            if page_tokens:
                for i, cell in enumerate(row.cells):
                    if not _supported_by(cell, page_tokens):
                        log.warning("docling cell dropped: words not on the page", extra={
                            "page": page.number, "code": code, "cell": collapse(cell)[:40]})
                        row.cells[i] = ""
            kept.append(row)
        return TableSegment(schema=segment.schema, rows=kept)
