"""Strategy cascade.

No single reader survives every PDF, so run the cheap deterministic ones, score
what each returns, and keep the best. The LLM reader is last and only sees pages
the deterministic ones could not read — it costs tokens and its output still has
to be verified against the page text.

This is the part that makes the extractor survive an unseen guide: a new layout
degrades to a different strategy rather than to zero rows.
"""
from __future__ import annotations

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
                 llm_strategy: TableStrategy | None = None):
        self._strategies = list(strategies) if strategies is not None else [
            RuledTableStrategy(), GeometricTableStrategy()]
        self._llm = llm_strategy

    def extract_page(self, page: PageView, carried: TableSchema | None = None) -> PageTable:
        best = PageTable(schema=carried, strategy="none")
        best_score = 0.0
        for strategy in self._strategies:
            try:
                result = strategy.extract(page, carried)
            except Exception as exc:                     # a broken page must not kill the document
                log.warning("table strategy failed", extra={
                    "strategy": strategy.name, "page": page.number, "error": str(exc)[:200]})
                continue
            score = result.score()
            if score > best_score:
                best, best_score = result, score
            if score >= GOOD_ENOUGH:
                break

        if best_score == 0.0 and self._llm is not None and _page_has_codes(page):
            log.info("falling back to llm table reader", extra={"page": page.number})
            try:
                fallback = self._llm.extract(page, carried)
                if fallback.score() > 0:
                    best = fallback
            except Exception as exc:
                log.warning("llm table strategy failed", extra={
                    "page": page.number, "error": str(exc)[:200]})

        log.debug("page read", extra={"page": page.number, "strategy": best.strategy,
                                      "rows": len(best.rows), "score": round(best_score, 2)})
        return best


def _page_has_codes(page: PageView) -> bool:
    from app.application.tables.models import CODE_RE
    return any(CODE_RE.match(w.text) for w in page.words)
