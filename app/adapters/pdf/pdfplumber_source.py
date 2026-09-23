"""PDF adapter. The only module that knows pdfplumber exists.

Exposes each page as words (with geometry), ruled tables and plain text, so the
table strategies can work on whichever representation survives in a given PDF.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import pdfplumber

from app.domain.errors import InputRejected


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    bold: bool

    @property
    def mid(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Rect:
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass
class PageView:
    number: int
    width: float
    height: float
    words: list[Word]
    text: str
    ruled_tables: list[list[list[str]]]
    rects: list[Rect] = field(default_factory=list)
    source_path: Path | None = None

    @property
    def has_text(self) -> bool:
        return bool(self.words)


class PdfPlumberSource:
    """Opens a PDF and yields PageViews. Raises InputRejected for a file that is
    not a readable PDF or has no text layer at all (scanned image, needs OCR)."""

    def __init__(self, path: str | Path, *, require_text: bool = True):
        self.path = Path(path)
        # A document-AI fallback can read a scanned page, so the caller relaxes
        # this rather than the source deciding on everyone's behalf.
        self.require_text = require_text

    def pages(self) -> Iterator[PageView]:
        try:
            pdf = pdfplumber.open(self.path)
        except Exception as exc:                       # corrupt, encrypted, not a PDF
            raise InputRejected(f"cannot open PDF: {exc}", stage="pdf_ingest") from exc

        empty = 0
        with pdf:
            for i, page in enumerate(pdf.pages, start=1):
                view = self._view(page, i, self.path)
                empty += 0 if view.has_text else 1
                yield view
            if self.require_text and empty and empty == len(pdf.pages):
                raise InputRejected(
                    "PDF has no extractable text layer (scanned image?); OCR is required",
                    stage="pdf_ingest", details={"pages": empty})

    @staticmethod
    def _view(page, number: int, source_path: Path | None = None) -> PageView:
        words = [
            Word(text=w["text"], x0=w["x0"], x1=w["x1"], top=w["top"], bottom=w["bottom"],
                 bold="bold" in (w.get("fontname") or "").lower())
            for w in page.extract_words(keep_blank_chars=False, extra_attrs=["fontname"])
        ]
        try:
            tables = [[[(c or "").replace("\n", " ").strip() for c in row] for row in tb]
                      for tb in page.extract_tables()]
        except Exception:                              # malformed table objects
            tables = []
        rects = [Rect(x0=r["x0"], x1=r["x1"], top=r["top"], bottom=r["bottom"])
                 for r in page.rects]
        return PageView(number=number, width=float(page.width), height=float(page.height),
                        words=words, text=page.extract_text() or "", ruled_tables=tables,
                        rects=rects, source_path=source_path)
