"""Strategy cascade.

No single reader survives every PDF, so run the cheap deterministic ones, score
what each returns, and keep the best. The LLM reader is last and only sees pages
the deterministic ones could not read — it costs tokens and its output still has
to be verified against the page text.

This is the part that makes the extractor survive an unseen guide: a new layout
degrades to a different strategy rather than to zero rows.
"""
from __future__ import annotations

import inspect
from typing import Protocol, Sequence

from app.adapters.pdf.pdfplumber_source import PageView
from app.application.tables.geometric import GeometricTableStrategy
from app.application.tables.models import PageTable, TableSchema
from app.application.tables.ruled import RuledTableStrategy
from app.logging_setup import get_logger

log = get_logger("extract.tables")
GOOD_ENOUGH = 0.8            # stop early when a strategy reads a page this cleanly


class TableStrategy(Protocol):
    name: str

    def extract(self, page: PageView, carried: TableSchema | None = None) -> PageTable: ...


class TableCascade:
    def __init__(self, strategies: Sequence[TableStrategy] | None = None,
                 llm_strategy: TableStrategy | None = None,
                 fallbacks: Sequence[TableStrategy] | None = None,
                 fallback_first: bool = False):
        self._strategies = list(strategies) if strategies is not None else [
            RuledTableStrategy(), GeometricTableStrategy()]
        # Fallbacks cost money or determinism, so they run only when the free,
        # reproducible readers found nothing on a page that clearly has codes.
        self._fallbacks = list(fallbacks or [])
        if llm_strategy is not None:
            self._fallbacks.append(llm_strategy)
        # Normally the fallback runs only where the deterministic readers fail.
        # A corpus that is mostly scans can invert that with document_ai_mode=always,
        # keeping the deterministic readers as the backup instead.
        self._fallback_first = fallback_first

    @staticmethod
    def _needs_fallback(page: PageView) -> bool:
        """Worth paying for: a page that clearly holds benefit codes but nothing
        could read, or a page with no text layer at all (a scan)."""
        return _page_has_codes(page) or not page.has_text

    @property
    def has_fallbacks(self) -> bool:
        """True when a reader exists that can handle a page with no text layer."""
        return bool(self._fallbacks)

    def start_document(self) -> None:
        """Per-document hook: fallbacks that meter themselves reset their budget."""
        for fallback in self._fallbacks:
            if hasattr(fallback, "reset_budget"):
                fallback.reset_budget()

    async def extract_page(self, page: PageView, carried: TableSchema | None = None) -> PageTable:
        """Async because a fallback may call a remote service; the deterministic
        strategies stay plain functions and are awaited only if they return an
        awaitable, so both kinds implement the same interface."""
        if self._fallback_first and self._fallbacks:
            for fallback in self._fallbacks:
                try:
                    result = await _maybe_await(fallback.extract(page, carried))
                except Exception as exc:
                    log.warning("primary reader failed; falling back to deterministic", extra={
                        "page": page.number, "strategy": fallback.name, "error": str(exc)[:200]})
                    continue
                if result.rows:
                    return result

        best = PageTable(schema=carried, strategy="none")
        best_score = 0.0
        for strategy in self._strategies:
            try:
                result = await _maybe_await(strategy.extract(page, carried))
            except Exception as exc:                     # a broken page must not kill the document
                log.warning("table strategy failed", extra={
                    "strategy": strategy.name, "page": page.number, "error": str(exc)[:200]})
                continue
            score = result.score()
            if score > best_score:
                best, best_score = result, score
            if score >= GOOD_ENOUGH:
                break

        if best_score == 0.0 and self._fallbacks and self._needs_fallback(page):
            for fallback in self._fallbacks:
                log.info("falling back", extra={"page": page.number, "strategy": fallback.name})
                try:
                    result = await _maybe_await(fallback.extract(page, carried))
                except Exception as exc:
                    log.warning("fallback strategy failed", extra={
                        "page": page.number, "strategy": fallback.name, "error": str(exc)[:200]})
                    continue
                if result.rows:
                    # Rows, not score: an OCR'd scan of short cells can score
                    # 0.0 and still be the only reading of that page.
                    return result
        

        log.debug("page read", extra={"page": page.number, "strategy": best.strategy,
                                      "rows": len(best.rows), "score": round(best_score, 2)})
        return best


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def _page_has_codes(page: PageView) -> bool:
    from app.application.tables.models import CODE_RE
    return any(CODE_RE.match(w.text) for w in page.words)
