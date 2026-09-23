"""Docling fallback: contract tests with a fake converter, plus an opt-in real run.

The unit tests never import docling — they drive the adapter through an injected
converter, so the suite stays fast and runs without model weights. The real
end-to-end run is gated behind RUN_DOCLING_E2E=1.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from app.adapters.document_ai.docling import DoclingTableStrategy, DoclingUnavailable
from app.adapters.llm.stub import StubLLMClient
from app.adapters.pdf.pdfplumber_source import PageView, Word
from app.api.deps import build_document_ai
from app.application.dental_guide import DentalGuidePipeline
from app.application.tables.cascade import TableCascade
from app.config import Settings

HEADER = ["Code", "Description", "Frequency", "In-network", "Out-of-network"]
BODY = [
    ["Diagnostic services", "", "", "", ""],                      # section heading row
    ["D0120", "Periodic oral evaluation", "2 per year", "100%", "80%"],
    ["D0274", "Bitewings, four images", "1 per year", "100%", "80%"],
    ["", "", "", "", ""],                                         # blank row
    ["not a code", "noise", "", "", ""],                          # dropped
]


# -- fakes standing in for a converted DoclingDocument ----------------------

class FakeFrame:
    def __init__(self, columns, rows):
        self.columns = columns
        self._rows = rows

    def itertuples(self, index=False):
        return iter(self._rows)


class FakeTable:
    def __init__(self, columns, rows, page_no=1, as_grid=False):
        self._frame = FakeFrame(columns, rows)
        self._as_grid = as_grid
        self.prov = [type("Prov", (), {"page_no": page_no})()]
        if as_grid:
            cell = lambda text: type("Cell", (), {"text": text})()
            self.data = type("Data", (), {"grid": [[cell(c) for c in columns]] +
                                                  [[cell(c) for c in row] for row in rows]})()

    def export_to_dataframe(self):
        if self._as_grid:
            raise RuntimeError("this docling version has no dataframe export")
        return self._frame


class FakeConverter:
    def __init__(self, tables=None, markdown: str | None = None, fail: Exception | None = None):
        self.calls = 0
        self._tables = tables
        self._markdown = markdown
        self._fail = fail

    def convert(self, source):
        self.calls += 1
        if self._fail:
            raise self._fail
        document = type("Doc", (), {
            "tables": self._tables or [],
            "export_to_markdown": lambda self=None: self._markdown if self else self._markdown,
        })()
        document.export_to_markdown = lambda: self._markdown or ""
        return type("Result", (), {"document": document})()


def scanned_pdf(path: Path, pages: int = 1) -> Path:
    c = canvas.Canvas(str(path))
    for _ in range(pages):
        c.rect(100, 400, 300, 200, fill=1)
        c.showPage()
    c.save()
    return path


def page_of(path, *, words=None, number=1) -> PageView:
    return PageView(number=number, width=612.0, height=792.0, words=words or [],
                    text=" ".join(w.text for w in (words or [])), ruled_tables=[], rects=[],
                    source_path=path)


def strategy(converter, **overrides) -> DoclingTableStrategy:
    return DoclingTableStrategy(Settings(document_ai_provider="docling", **overrides), converter)


# -- reading ----------------------------------------------------------------

async def test_reads_a_page_the_deterministic_readers_cannot(tmp_path):
    reader = strategy(FakeConverter([FakeTable(HEADER, BODY)]))
    table = await reader.extract(page_of(scanned_pdf(tmp_path / "scan.pdf")))

    assert [r.code for r in table.rows] == ["D0120", "D0274"]
    assert table.schema.labels == HEADER
    assert table.rows[0].group == "Diagnostic services"       # heading row, not a benefit row
    assert table.strategy == "docling"


async def test_falls_back_to_the_cell_grid_when_dataframe_export_is_unavailable(tmp_path):
    reader = strategy(FakeConverter([FakeTable(HEADER, BODY, as_grid=True)]))
    table = await reader.extract(page_of(scanned_pdf(tmp_path / "scan.pdf")))

    assert [r.code for r in table.rows] == ["D0120", "D0274"]


async def test_falls_back_to_markdown_when_no_tables_are_detected(tmp_path):
    markdown = ("| Code | Description |\n| --- | --- |\n"
                "| D0120 | Periodic oral evaluation |\n")
    reader = strategy(FakeConverter(tables=[], markdown=markdown))
    table = await reader.extract(page_of(scanned_pdf(tmp_path / "scan.pdf")))

    assert [r.code for r in table.rows] == ["D0120"]


async def test_rows_are_routed_to_the_page_docling_found_them_on(tmp_path):
    reader = strategy(FakeConverter([FakeTable(HEADER, BODY, page_no=2)]))
    path = scanned_pdf(tmp_path / "scan.pdf", pages=2)

    assert (await reader.extract(page_of(path, number=1))).rows == []
    assert len((await reader.extract(page_of(path, number=2))).rows) == 2


# -- trust ------------------------------------------------------------------

async def test_rows_whose_code_is_not_on_a_readable_page_are_dropped(tmp_path):
    reader = strategy(FakeConverter([FakeTable(HEADER, BODY)]))
    words = [Word(text="D0120", x0=0, x1=10, top=0, bottom=10, bold=False)]
    table = await reader.extract(page_of(scanned_pdf(tmp_path / "s.pdf"), words=words))

    assert [r.code for r in table.rows] == ["D0120"]           # D0274 is not on this page


async def test_malformed_rows_never_reach_the_output(tmp_path):
    rows = [["NOTACODE", "something", "", "", ""], ["D0120", "fine", "", "", ""]]
    reader = strategy(FakeConverter([FakeTable(HEADER, rows)]))
    table = await reader.extract(page_of(scanned_pdf(tmp_path / "s.pdf")))

    assert [r.code for r in table.rows] == ["D0120"]


# -- cost and failure -------------------------------------------------------

async def test_a_document_is_converted_once_no_matter_how_many_pages_ask(tmp_path):
    converter = FakeConverter([FakeTable(HEADER, BODY)])
    reader = strategy(converter)
    path = scanned_pdf(tmp_path / "scan.pdf", pages=3)
    for number in (1, 2, 3):
        await reader.extract(page_of(path, number=number))

    assert converter.calls == 1                                # conversion is the expensive part


async def test_page_budget_caps_what_one_document_can_cost(tmp_path):
    reader = strategy(FakeConverter([FakeTable(HEADER, BODY)]), document_ai_max_pages=2)
    path = scanned_pdf(tmp_path / "scan.pdf", pages=4)
    results = [await reader.extract(page_of(path, number=n)) for n in (1, 2, 3, 4)]

    assert sum(1 for r in results if r.rows) == 1              # page 1 has the rows
    reader.reset_budget()
    assert (await reader.extract(page_of(path, number=1))).rows                # next document

async def test_conversion_failure_is_reported_not_swallowed(tmp_path):
    reader = strategy(FakeConverter(fail=RuntimeError("model weights missing")))
    with pytest.raises(DoclingUnavailable):
        await reader.extract(page_of(scanned_pdf(tmp_path / "s.pdf")))


async def test_a_page_with_no_source_file_is_skipped():
    reader = strategy(FakeConverter([FakeTable(HEADER, BODY)]))
    assert (await reader.extract(page_of(None))).rows == []


# -- placement in the cascade ----------------------------------------------

async def test_fallback_is_not_called_when_a_deterministic_reader_works(tmp_path):
    converter = FakeConverter([FakeTable(HEADER, BODY)])
    ruled = PageView(number=1, width=612.0, height=792.0, words=[], text="D0120",
                     ruled_tables=[[["Code", "Description", "Frequency"],
                                    ["D0120", "Periodic oral evaluation", "2 per year"]]],
                     rects=[], source_path=scanned_pdf(tmp_path / "s.pdf"))
    result = await TableCascade(fallbacks=[strategy(converter)]).extract_page(ruled)

    assert result.strategy == "ruled"
    assert converter.calls == 0                                # nothing was spent


async def test_a_failing_fallback_degrades_instead_of_killing_the_document(tmp_path):
    cascade = TableCascade(fallbacks=[strategy(FakeConverter(fail=RuntimeError("boom")))])
    result = await cascade.extract_page(page_of(scanned_pdf(tmp_path / "s.pdf")))

    assert result.rows == []


async def test_scanned_document_is_refused_without_the_fallback_and_read_with_it(tmp_path, settings):
    path = scanned_pdf(tmp_path / "scan.pdf")

    with pytest.raises(Exception) as exc:
        await DentalGuidePipeline(settings, StubLLMClient()).run(path)
    assert "text layer" in str(exc.value)

    cascade = TableCascade(fallbacks=[strategy(FakeConverter([FakeTable(HEADER, BODY)]))])
    result = await DentalGuidePipeline(settings, StubLLMClient(), cascade=cascade).run(path)

    assert [r.dental_code for r in result.rows] == ["D0120", "D0274"]
    assert result.rows[0].in_network == "100%"
    assert result.rows[0].strategy == "docling"
    assert any(f.rule == "dental_guide.rows_not_locally_verifiable"
               for f in result.validation.flags)


# -- wiring -----------------------------------------------------------------

def test_fallback_is_off_unless_configured():
    assert build_document_ai(Settings()) is None
    assert build_document_ai(Settings(document_ai_provider="none")) is None
    assert build_document_ai(Settings(document_ai_provider="something-else")) is None


def test_missing_docling_install_degrades_instead_of_failing(monkeypatch):
    def explode() -> None:
        raise DoclingUnavailable("docling is not installed", stage="config")

    monkeypatch.setattr(DoclingTableStrategy, "_check_available", staticmethod(explode))
    assert build_document_ai(Settings(document_ai_provider="docling")) is None


# -- the real thing, opt-in -------------------------------------------------

@pytest.mark.skipif(not os.getenv("RUN_DOCLING_E2E"),
                    reason="set RUN_DOCLING_E2E=1 to run docling for real (downloads models)")
async def test_real_docling_reads_a_scanned_guide(tmp_path, settings):
    pytest.importorskip("docling")
    path = scanned_pdf(tmp_path / "scan.pdf")
    reader = DoclingTableStrategy(Settings(document_ai_provider="docling"))
    result = await reader.extract(page_of(path))
    assert result.strategy == "docling"          # no rows expected from a blank scan
