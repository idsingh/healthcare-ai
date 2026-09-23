"""Table-layer value objects.

A `PageTable` is one page's worth of reconstructed rows plus the schema that
produced them. The schema carries across pages because benefit tables run for
tens of pages and only the first page repeats a full header.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CODE_RE = re.compile(r"^[A-Z]\d{4}[A-Z]?$")


def find_code(text: str) -> str | None:
    """First dental procedure code in a cell, e.g. 'D0120'. Domain knowledge
    (CDT code shape), not document knowledge."""
    for token in re.split(r"[\s,;/]+", (text or "").strip()):
        if CODE_RE.match(token):
            return token
    return None


@dataclass(frozen=True)
class Column:
    label: str
    left: float
    right: float


@dataclass
class TableSchema:
    columns: list[Column]
    source: str                      # which strategy produced it
    group: str | None = None         # last section heading seen, carried across pages

    @property
    def labels(self) -> list[str]:
        return [c.label for c in self.columns]

    def richness(self) -> int:
        """How much header text this schema actually captured; used to keep the
        best header when later pages repeat only part of it."""
        return sum(len(c.label) for c in self.columns)


@dataclass
class RawRow:
    cells: list[str]
    group: str | None = None
    page: int = 0
    y: float = 0.0

    @property
    def code(self) -> str | None:
        return next((c for c in (find_code(cell) for cell in self.cells) if c), None)


@dataclass
class PageTable:
    schema: TableSchema | None
    rows: list[RawRow] = field(default_factory=list)
    strategy: str = "none"

    def score(self) -> float:
        """Quality of a strategy's reading of one page: the share of rows that
        carry a code and some description text. Used to choose between
        strategies rather than trusting any single one."""
        if not self.rows:
            return 0.0
        good = sum(1 for r in self.rows
                   if r.code and any(len(c.strip()) > 3 for c in r.cells[1:] if c))
        return good / len(self.rows)
