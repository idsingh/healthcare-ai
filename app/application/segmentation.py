"""Stage 2 — segmentation.

The layout hazard lives here, not in the extractors: the source is a two-column
benefits table flattened into reading order, so cost-column sentences land in
the middle of the benefit-column code list. Blocks are cut on package headings
(attachment), and lines are tagged with their column (prompt clarity).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.application.preprocess import NormalizedDocument

PACKAGE_ANCHOR = re.compile(
    r"^\s*(?:Optional\s+)?supplemental\s+(?:benefit\s+)?package\s+(\d+)\s*[-:]\s*(.+)$",
    re.I | re.M)

COST_CUES = (
    "the plan will pay up to", "you pay", "coverage is available from",
    "talk to your provider", "exclusions & limitations", "exclusions and limitations",
    "claims for covered benefits", "your costs for these services",
    "services must be rendered", "you must pay any extra",
)
CODE_LINE = re.compile(r"^\s*(?:[•\-*]\s*)?D\d{4}\b")
CONTINUATION = re.compile(r"^\s*[a-z(\[]")          # wrapped line, not a new item


def logical_lines(text: str) -> list[str]:
    """Undo arbitrary PDF line wrapping.

    A line that starts lowercase continues the previous one — that is how
    '• D0150 - Comprehensive oral evaluation - new or' / 'established patient'
    becomes one bullet again, while 'Two fluoride treatments per year' stays a
    separate item.
    """
    out: list[str] = []
    for line in (ln.strip() for ln in text.splitlines()):
        if not line:
            continue
        if out and CONTINUATION.match(line):
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
    return out


@dataclass(frozen=True)
class Block:
    block_id: str
    kind: str                 # "header" | "package"
    text: str                 # line-preserving slice, for the prompt
    flat: str                 # collapsed slice, for scanners and offsets
    start: int                # flat offset
    end: int                  # flat offset
    ordinal: int | None = None
    heading: str | None = None
    pages: tuple[int, ...] = ()

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)


class Segmenter:
    def segment(self, doc: NormalizedDocument) -> list[Block]:
        anchors = list(PACKAGE_ANCHOR.finditer(doc.text))
        blocks: list[Block] = []

        header_end = anchors[0].start() if anchors else len(doc.text)
        if header_end > 0:
            blocks.append(self._build(doc, "header", 0, header_end, kind="header"))

        for i, m in enumerate(anchors):
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(doc.text)
            blocks.append(self._build(
                doc, f"pkg_{m.group(1)}", m.start(), end, kind="package",
                ordinal=int(m.group(1)), heading=m.group(0).strip()))
        return blocks

    def _build(self, doc: NormalizedDocument, block_id: str, t_start: int, t_end: int,
               *, kind: str, ordinal: int | None = None, heading: str | None = None) -> Block:
        f_start, f_end = doc.flat_span_of_text(t_start, t_end)
        pages = sorted({p for p in (doc.page_at(f_start), doc.page_at(max(f_end - 1, f_start)))
                        if p is not None})
        return Block(
            block_id=block_id, kind=kind, text=doc.text[t_start:t_end].strip(),
            flat=doc.flat[f_start:f_end], start=f_start, end=f_end,
            ordinal=ordinal, heading=heading, pages=tuple(pages),
        )


def classify_columns(block: Block) -> dict[str, list[str]]:
    """Split a block's lines into the two original table columns.

    Cue phrases decide first, code bullets second, then run-length smoothing
    re-assigns a single stray line sitting inside a long run of the other
    column. Lines that stay uncertain are reported so the prompt can present
    them under both headings rather than guessing.
    """
    lines = logical_lines(block.text)
    labels: list[str] = []
    for line in lines:
        low = line.lower()
        if any(cue in low for cue in COST_CUES):
            labels.append("cost")
        elif CODE_LINE.match(line):
            labels.append("benefit")
        else:
            labels.append("?")

    for i, label in enumerate(labels):        # smoothing
        if label != "?":
            continue
        window = [x for x in labels[max(0, i - 3): i + 4] if x != "?"]
        labels[i] = max(set(window), key=window.count) if window else "benefit"

    out: dict[str, list[str]] = {"benefit": [], "cost": []}
    for line, label in zip(lines, labels):
        out[label].append(line)
    return out
