"""Stage 1 — preprocess.

Produces two views of the document and the mapping between them:

  text  line structure preserved   -> segmentation, prompting
  flat  whitespace collapsed       -> evidence offsets, groundedness checks

Offsets in the output are `flat` offsets: layout-independent, so they survive
re-extraction of the same PDF with a different text extractor.
"""
from __future__ import annotations

import bisect
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from app.config import Settings
from app.domain.errors import InputRejected

DASHES = {"–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-", "­": ""}
PAGE_ANCHOR = re.compile(r"^[ \t]*(\d{1,4})[ \t]+(?:19|20)\d{2}\s+Evidence of Coverage", re.M)
BOILERPLATE_MIN_LEN = 15
BOILERPLATE_MIN_PAGES = 2
HEAD_ZONE_LINES = 5     # running headers live at the top of a page
FOOT_ZONE_LINES = 3     # ... and footers at the bottom


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(DASHES.get(ch, ch) for ch in text)


def collapse(text: str) -> str:
    """The canonical comparison form. Used by groundedness checks on both sides,
    so 'grounded' means exactly one thing everywhere in the service."""
    return re.sub(r"\s+", " ", fold(text)).strip()


@dataclass
class NormalizedDocument:
    text: str
    flat: str
    text_to_flat: list[int]
    page_numbers: list[int | None]
    page_text_starts: list[int]
    content_sha256: str
    stats: dict = field(default_factory=dict)

    def flat_span_of_text(self, start: int, end: int) -> tuple[int, int]:
        if not self.text_to_flat:
            return (0, 0)
        end = min(end, len(self.text_to_flat))
        return (self.text_to_flat[start], self.text_to_flat[end - 1] + 1)

    def page_at(self, flat_pos: int) -> int | None:
        """Page number covering a flat offset (bisect over page starts)."""
        text_pos = bisect.bisect_left(self.text_to_flat, flat_pos)
        idx = bisect.bisect_right(self.page_text_starts, text_pos) - 1
        return self.page_numbers[idx] if 0 <= idx < len(self.page_numbers) else None

    def locate(self, quote: str, within: tuple[int, int] | None = None) -> tuple[int, int] | None:
        """Find a quote in flat space, preferring the given window so a phrase
        repeated in two packages cannot anchor to the wrong one."""
        needle = collapse(quote)
        if not needle:
            return None
        if within:
            found = self.flat.find(needle, within[0], within[1])
            if found != -1:
                return (found, found + len(needle))
        found = self.flat.find(needle)
        return (found, found + len(needle)) if found != -1 else None


class Preprocessor:
    """Validate -> page split -> boilerplate strip -> normalize -> flatten."""

    def __init__(self, settings: Settings):
        self._s = settings

    def run(self, raw: str) -> NormalizedDocument:
        self._validate(raw)
        pages = self._split_pages(raw)
        boilerplate = self._boilerplate_lines(pages)

        chunks: list[str] = []
        page_numbers: list[int | None] = []
        page_text_starts: list[int] = []
        seen: set[str] = set()
        dropped = 0
        cursor = 0

        for number, body in pages:
            kept: list[str] = []
            lines = body.splitlines()
            for idx, line in enumerate(lines):
                key = collapse(line)
                in_zone = idx < HEAD_ZONE_LINES or idx >= len(lines) - FOOT_ZONE_LINES
                if in_zone and key in boilerplate:
                    if key in seen:
                        dropped += 1
                        continue
                    seen.add(key)   # keep the first copy: it carries plan name and year
                kept.append(re.sub(r"[ \t]+", " ", fold(line)).strip())
            page_text = "\n".join(ln for ln in kept if ln)
            if not page_text:
                continue
            page_numbers.append(number)
            page_text_starts.append(cursor)
            chunks.append(page_text)
            cursor += len(page_text) + 1

        text = "\n".join(chunks)
        flat, text_to_flat = self._flatten(text)
        if len(flat) < self._s.min_input_chars:
            raise InputRejected("document has no usable text after preprocessing", stage="preprocess")

        return NormalizedDocument(
            text=text,
            flat=flat,
            text_to_flat=text_to_flat,
            page_numbers=page_numbers,
            page_text_starts=page_text_starts,
            content_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            stats={"pages": len(page_numbers), "boilerplate_lines_dropped": dropped,
                   "chars_raw": len(raw), "chars_normalized": len(flat)},
        )

    # -- input validation ---------------------------------------------------
    def _validate(self, raw: str) -> None:
        if not raw or not raw.strip():
            raise InputRejected("text is empty", stage="input_validation")
        size = len(raw.encode("utf-8"))
        if size > self._s.max_input_bytes:
            raise InputRejected(
                f"text is {size} bytes, limit is {self._s.max_input_bytes}",
                stage="input_validation", details={"bytes": size, "limit": self._s.max_input_bytes})
        if len(raw.strip()) < self._s.min_input_chars:
            raise InputRejected(
                f"text is {len(raw.strip())} chars, minimum is {self._s.min_input_chars}",
                stage="input_validation")
        printable = sum(1 for ch in raw if ch.isprintable() or ch.isspace())
        ratio = printable / len(raw)
        if ratio < self._s.min_printable_ratio:
            raise InputRejected(
                f"text is {ratio:.0%} printable, minimum is {self._s.min_printable_ratio:.0%} "
                "(binary or mis-decoded input?)",
                stage="input_validation", details={"printable_ratio": round(ratio, 4)})

    # -- helpers ------------------------------------------------------------
    def _split_pages(self, raw: str) -> list[tuple[int | None, str]]:
        anchors = list(PAGE_ANCHOR.finditer(raw))
        if not anchors:
            return [(None, raw)]
        pages: list[tuple[int | None, str]] = []
        if anchors[0].start() > 0:
            pages.append((None, raw[: anchors[0].start()]))
        for i, m in enumerate(anchors):
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(raw)
            pages.append((int(m.group(1)), raw[m.start(): end]))
        return pages

    def _boilerplate_lines(self, pages: list[tuple[int | None, str]]) -> set[str]:
        """Repetition *in the header/footer zone*, not hardcoded patterns.

        Zone-restricted on purpose: a naive frequency filter also deletes real
        content that legitimately repeats — the same CDT code bullets and the
        same 'You pay no copay ...' sentence appear in both packages of this
        document, and dropping the second copy silently empties package 2.
        """
        counts: dict[str, int] = {}
        for _, body in pages:
            lines = body.splitlines()
            zone = lines[:HEAD_ZONE_LINES] + lines[-FOOT_ZONE_LINES:]
            for key in {collapse(ln) for ln in zone}:
                if len(key) >= BOILERPLATE_MIN_LEN:
                    counts[key] = counts.get(key, 0) + 1
        return {k for k, n in counts.items() if n >= BOILERPLATE_MIN_PAGES}

    @staticmethod
    def _flatten(text: str) -> tuple[str, list[int]]:
        out: list[str] = []
        mapping: list[int] = []
        for ch in text:
            if ch.isspace():
                if not out or out[-1] == " ":
                    mapping.append(max(len(out) - 1, 0))
                    continue
                ch = " "
            mapping.append(len(out))
            out.append(ch)
        flat = "".join(out)
        trimmed = flat.rstrip()
        return trimmed, [min(i, max(len(trimmed) - 1, 0)) for i in mapping]
