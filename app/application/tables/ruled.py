"""Ruled strategy: use the table grid the PDF already draws.

When a guide renders its benefit table with ruling lines or filled cells,
pdfplumber recovers the cells exactly, including multi-line ones. Preferred
whenever it yields a usable reading, because it needs no geometry guessing.
"""
from __future__ import annotations

import re

from app.adapters.pdf.pdfplumber_source import PageView
from app.application.tables.geometric import HEADER_TOKENS
from app.application.tables.models import (
    Column, PageTable, RawRow, TableSchema, TableSegment, find_code)


def _looks_like_header(cells: list[str]) -> bool:
    if any(find_code(c) for c in cells):
        return False
    tokens = set(re.split(r"\W+", " ".join(cells).lower()))
    return len(tokens & HEADER_TOKENS) >= 2


def _is_heading(cells: list[str]) -> bool:
    """A section heading row: text in the first cell only."""
    return bool(cells and cells[0].strip()) and not any(c.strip() for c in cells[1:])


class RuledTableStrategy:
    name = "ruled"

    def extract(self, page: PageView, carried: TableSchema | None = None) -> PageTable:
        schema = carried
        group = carried.group if carried else None
        segments: list[TableSegment] = []
        rows: list[RawRow] = []

        def close() -> None:
            if rows:
                segments.append(TableSegment(schema=schema, rows=list(rows)))
                rows.clear()

        for table in page.ruled_tables:
            for raw in table:
                cells = [(c or "").strip() for c in raw]
                if not any(cells):
                    continue
                if _looks_like_header(cells):
                    close()                       # a new header starts a new table
                    schema = TableSchema(
                        columns=[Column(label=c, left=i, right=i + 1) for i, c in enumerate(cells)],
                        source=self.name, group=group)
                    continue
                if _is_heading(cells):
                    group = cells[0]
                    continue
                if find_code(cells[0] if cells else ""):
                    rows.append(RawRow(cells=cells, group=group, page=page.number, y=len(rows)))
        close()

        if schema:
            schema.group = group
        return PageTable(schema=schema, rows=[r for s in segments for r in s.rows],
                         strategy=self.name, segments=segments or [TableSegment(schema, [])])
