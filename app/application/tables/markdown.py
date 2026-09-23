"""Markdown table -> rows.

Document-AI services return a page as markdown. This turns the pipe tables in
that markdown back into the same `TableSegment` shape the deterministic
strategies produce, so everything downstream — column mapping, row assembly,
validation — is identical no matter which reader won.
"""
from __future__ import annotations

import re

from app.application.tables.models import Column, RawRow, TableSchema, TableSegment, find_code

ALIGNMENT_ROW = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
HEADING = re.compile(r"^\s*(#{1,6}\s+|\*\*)(?P<text>[^*#]+?)(\*\*)?\s*$")


def split_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [re.sub(r"\s+", " ", c).strip() for c in cells]


def is_table_row(line: str) -> bool:
    return line.count("|") >= 2


def parse_markdown_tables(markdown: str, page_number: int = 0,
                          group: str | None = None) -> list[TableSegment]:
    """Every pipe table becomes a segment. Text between tables is kept only when
    it looks like a section heading, because that is the benefit group."""
    segments: list[TableSegment] = []
    schema: TableSchema | None = None
    rows: list[RawRow] = []
    current_group = group

    def close() -> None:
        nonlocal rows
        if rows:
            segments.append(TableSegment(schema=schema, rows=rows))
            rows = []

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ALIGNMENT_ROW.match(stripped):
            continue
        if is_table_row(stripped):
            cells = split_row(stripped)
            if find_code(cells[0] if cells else "") or any(find_code(c) for c in cells):
                rows.append(RawRow(cells=cells, group=current_group, page=page_number,
                                   y=float(len(rows))))
            elif any(cells):
                close()                                  # a header starts a new table
                schema = TableSchema(
                    columns=[Column(label=c, left=i, right=i + 1) for i, c in enumerate(cells)],
                    source="document_ai", group=current_group)
            continue
        if match := HEADING.match(stripped):
            current_group = match.group("text").strip()
            continue
        if len(stripped) < 60 and not stripped.endswith("."):
            current_group = stripped                     # a bare line above a table
    close()

    if schema:
        schema.group = current_group
    return segments
