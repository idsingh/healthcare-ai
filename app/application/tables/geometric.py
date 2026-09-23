"""Geometric strategy: rebuild a table from word positions.

For PDFs whose benefit table is drawn without ruling lines (very common), so
pdfplumber's line-based extraction returns nothing usable. Works from three
signals that hold regardless of layout:

  * a code column — cells matching the CDT code shape anchor each row;
  * whitespace columns — x ranges that stay empty across every data line;
  * vertical proximity — a wrapped line belongs to the nearest code line, and
    never crosses a section heading.
"""
from __future__ import annotations

from collections import defaultdict

from app.adapters.pdf.pdfplumber_source import PageView, Word
from app.application.tables.models import CODE_RE, Column, PageTable, RawRow, TableSchema

HEADER_TOKENS = {
    "code", "codes", "description", "descriptions", "benefit", "benefits", "frequency",
    "limitation", "limitations", "periodicity", "coverage", "network", "category", "service",
    "prior", "authorization", "required", "ada", "procedure", "pays", "member", "cost", "share",
    "copay", "copayment", "tier", "plan",
}
MAX_HEADER_WORDS = 12
MIN_COLUMN_GAP = 5
LINE_TOLERANCE = 3
MIN_SPAN_HEIGHT = 20     # a cell rectangle taller than this may span several rows
TOP_MARGIN = 0.06        # running headers live here
BOTTOM_MARGIN = 0.95     # ... and footers here


def group_lines(words: list[Word], tolerance: float = LINE_TOLERANCE) -> list[list[Word]]:
    buckets: dict[int, list[Word]] = defaultdict(list)
    for w in words:
        buckets[round(w.top / tolerance)].append(w)
    return [sorted(ws, key=lambda w: w.x0) for _, ws in sorted(buckets.items())]


def header_score(line: list[Word]) -> int:
    """How header-like a line is. Substring matching on purpose: the stacked
    label 'In-network' must score on 'network', or the second line of a
    two-line header is never merged into it."""
    words = {w.text.lower().strip(":?,.") for w in line}
    return sum(1 for w in words if any(tok in w for tok in HEADER_TOKENS))


def _pure_header_line(line: list[Word]) -> bool:
    """Every word is header vocabulary — so 'In-network Out-of-network' merges
    into the header, while a section heading that happens to sit next to it
    ('Endodontic restorative services (continued)') does not."""
    return bool(line) and all(
        any(tok in w.text.lower().strip(":?,.") for tok in HEADER_TOKENS) for w in line)


def has_code(line: list[Word]) -> bool:
    return any(CODE_RE.match(w.text) for w in line)


def whitespace_columns(words: list[Word], width: float) -> list[tuple[float, float]]:
    """Column bands are the x ranges separated by vertical whitespace that no
    data line crosses."""
    occupied = [False] * (int(width) + 2)
    for w in words:
        for x in range(int(w.x0), min(int(w.x1) + 1, int(width))):
            occupied[x] = True
    gaps, start = [], None
    for x, filled in enumerate(occupied):
        if not filled and start is None:
            start = x
        elif filled and start is not None:
            if x - start >= MIN_COLUMN_GAP and start > 0:
                gaps.append((start, x))
            start = None
    cuts = [0.0] + [(a + b) / 2 for a, b in gaps] + [float(width)]
    return [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]


class GeometricTableStrategy:
    name = "geometric"

    def extract(self, page: PageView, carried: TableSchema | None = None) -> PageTable:
        if not page.words:
            return PageTable(schema=carried, strategy=self.name)
        lines = group_lines(page.words)
        code_lines = [l for l in lines if has_code(l)]
        if not code_lines:
            return PageTable(schema=carried, strategy=self.name)

        code_right = max(w.x1 for l in code_lines for w in l if CODE_RE.match(w.text))
        header_index, header_line = self._find_header(lines, code_lines)
        first_code_top = min(l[0].top for l in code_lines)
        top = (header_line[0].bottom + 1 if header_line else first_code_top - 30)
        body = [l for l in lines if l[0].top >= top]

        def is_furniture(line: list[Word]) -> bool:
            """Running header, footer or page number: lives in a page margin and
            carries no code. Excluded from headings, column geometry and rows
            alike, so it can neither become a benefit group nor pollute a cell."""
            in_margin = (line[0].top < page.height * TOP_MARGIN
                         or line[0].bottom > page.height * BOTTOM_MARGIN)
            return in_margin and not has_code(line)

        def is_heading(line: list[Word]) -> bool:
            """Section headings start at the left edge, in the code column, and
            carry no code: 'Exams', 'Restorations (fillings)'. Page furniture in
            the top and bottom margins is never a heading."""
            return (not has_code(line) and line[0].x0 <= code_right + 2
                    and not is_furniture(line))

        # Column bands are the whitespace that BOTH the data rows and the header
        # leave empty. Data alone loses a column that happens to be empty on this
        # page; the header alone cuts through data that wraps past its label.
        data_words = [w for l in body if not is_heading(l) and not is_furniture(l) for w in l]
        column_words = data_words + (list(header_line) if header_line else [])
        bands = whitespace_columns(column_words, page.width)
        labels = self._labels(bands, lines, header_index, header_line)
        schema = TableSchema(columns=[Column(label=lab, left=lo, right=hi)
                                      for lab, (lo, hi) in zip(labels, bands)],
                             source=self.name, group=carried.group if carried else None)
        schema = self._keep_richer_header(schema, carried, len(bands))

        rows = self._bind_rows(body, bands, schema, is_heading, page.number, is_furniture)
        self._apply_spanning_cells(page, bands, rows)
        return PageTable(schema=schema, rows=rows, strategy=self.name)

    def _apply_spanning_cells(self, page: PageView, bands, rows: list[RawRow]) -> None:
        """A cell drawn as one tall rectangle covering several rows applies to all
        of them — that is what a merged cell means. Without this, a frequency
        stated once for a block of codes lands on a single arbitrary row."""
        if not rows:
            return
        for rect in page.rects:
            covered = [r for r in rows if rect.top <= r.y <= rect.bottom]
            if rect.height < MIN_SPAN_HEIGHT or len(covered) < 2:
                continue
            band_index = next((i for i, (lo, hi) in enumerate(bands)
                               if lo <= (rect.x0 + rect.x1) / 2 < hi), None)
            if band_index is None:
                continue
            text = " ".join(w.text for w in sorted(
                (w for w in page.words
                 if rect.x0 <= w.mid <= rect.x1 and rect.top <= w.top <= rect.bottom),
                key=lambda w: (round(w.top), w.x0))).strip()
            if not text:
                continue
            for row in covered:
                row.cells[band_index] = text

    # -- header -------------------------------------------------------------
    def _find_header(self, lines, code_lines):
        limit = min(l[0].top for l in code_lines)
        best = None
        for i, line in enumerate(lines):
            if line[0].top >= limit or len(line) > MAX_HEADER_WORDS:
                continue
            score = header_score(line)
            if score >= 2 and (best is None or score >= best[0]):
                best = (score, i, line)
        return (best[1], best[2]) if best else (None, None)

    def _labels(self, bands, lines, header_index, header_line) -> list[str]:
        if header_line is None:
            return [""] * len(bands)
        words = list(header_line)
        for j in (header_index - 1, header_index + 1):     # stacked header labels
            if 0 <= j < len(lines) and len(lines[j]) <= MAX_HEADER_WORDS \
                    and _pure_header_line(lines[j]) \
                    and abs(lines[j][0].top - header_line[0].top) < 30:
                words += lines[j]
        labels = []
        for lo, hi in bands:
            in_band = [w for w in words if lo <= w.mid < hi]
            labels.append(" ".join(w.text for w in sorted(in_band, key=lambda w: (round(w.top), w.x0))))
        return labels

    @staticmethod
    def _keep_richer_header(schema: TableSchema, carried: TableSchema | None, bands: int) -> TableSchema:
        """Continuation pages often repeat only part of the header ('coverage'
        instead of 'In-network coverage'). Keep the fullest label seen for each
        column so the canonical mapping does not degrade mid-document."""
        if not carried or len(carried.columns) != bands:
            return schema
        merged = [Column(label=(old.label if len(old.label) > len(new.label) else new.label),
                         left=new.left, right=new.right)
                  for old, new in zip(carried.columns, schema.columns)]
        return TableSchema(columns=merged, source=schema.source, group=schema.group)

    # -- rows ---------------------------------------------------------------
    def _bind_rows(self, body, bands, schema, is_heading, page_number, is_furniture) -> list[RawRow]:
        code_tops = [l[0].top for l in body if has_code(l) and not is_furniture(l)]
        heading_tops = [l[0].top for l in body if is_heading(l)]
        rows: dict[float, RawRow] = {}
        order: list[RawRow] = []
        group = schema.group

        for line in body:
            if is_heading(line):
                group = " ".join(w.text for w in line).strip()
                continue
            if is_furniture(line):              # page number, footer, running header
                continue
            cells = self._cells(line, bands)
            if not any(cells):
                continue
            top = line[0].top
            if has_code(line):
                anchor = top
            else:
                candidates = [c for c in code_tops
                              if not any(min(c, top) < h < max(c, top) for h in heading_tops)]
                if not candidates:
                    continue
                anchor = min(candidates, key=lambda c: abs(c - top))
            row = rows.get(anchor)
            if row is None:
                row = RawRow(cells=[""] * len(bands), group=group, page=page_number, y=anchor)
                rows[anchor] = row
                order.append(row)
            if has_code(line) and not row.group:
                row.group = group
            for i, cell in enumerate(cells):
                if cell:
                    row.cells[i] = (row.cells[i] + " " + cell).strip()

        schema.group = group
        return sorted(order, key=lambda r: r.y)

    @staticmethod
    def _cells(line: list[Word], bands) -> list[str]:
        cells = [""] * len(bands)
        for w in line:
            for i, (lo, hi) in enumerate(bands):
                if lo <= w.mid < hi:
                    cells[i] = (cells[i] + " " + w.text).strip()
                    break
        return cells
