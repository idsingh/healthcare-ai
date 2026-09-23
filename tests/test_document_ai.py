"""Mistral Document AI fallback: driven against a mock transport, never the network.

The point of these tests is the contract around the service, not the service:
it runs only when deterministic readers fail, its output is verified locally,
its failures degrade, and it cannot spend unbounded money.
"""
from __future__ import annotations

import json

import httpx
import pytest
from reportlab.pdfgen import canvas

from app.adapters.document_ai.mistral import MistralDocumentAIStrategy
from app.adapters.llm.stub import StubLLMClient
from app.adapters.pdf.pdfplumber_source import PageView, Word
from app.application.dental_guide import DentalGuidePipeline
from app.application.tables.cascade import TableCascade
from app.application.tables.markdown import parse_markdown_tables
from app.config import Settings
from app.domain.errors import LLMOutputError, LLMPermanentError, LLMTransientError

MARKDOWN = """## Diagnostic Services

| Code | Description | Frequency | In-network | Out-of-network |
| --- | --- | --- | --- | --- |
| D0120 | Periodic oral evaluation | 2 per year | 100% | 80% |
| D0274 | Bitewings, four images | 1 per year | 100% | 80% |
"""


def ocr_response(markdown: str = MARKDOWN) -> httpx.Response:
    return httpx.Response(200, json={"pages": [{"index": 0, "markdown": markdown}]})


def settings_with_key(**overrides) -> Settings:
    return Settings(document_ai_provider="mistral", mistral_api_key="test-key",
                    retry_base_delay_seconds=0.001, retry_max_delay_seconds=0.002, **overrides)


def strategy(handler, **overrides) -> MistralDocumentAIStrategy:
    s = settings_with_key(**overrides)
    return MistralDocumentAIStrategy(s, httpx.AsyncClient(
        base_url=s.mistral_base_url, transport=httpx.MockTransport(handler)))


def scanned_pdf(path, pages: int = 1):
    """Pages with no text layer: exactly what the deterministic readers cannot do."""
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


# -- markdown parsing -------------------------------------------------------

def test_markdown_tables_become_rows_with_a_schema():
    segments = parse_markdown_tables(MARKDOWN, page_number=3)
    assert len(segments) == 1
    assert segments[0].schema.labels == ["Code", "Description", "Frequency",
                                         "In-network", "Out-of-network"]
    assert [r.code for r in segments[0].rows] == ["D0120", "D0274"]
    assert segments[0].rows[0].group == "Diagnostic Services"
    assert segments[0].rows[0].page == 3


def test_two_markdown_tables_stay_separate():
    two = MARKDOWN + "\n| Code | Description |\n| --- | --- |\n| D2740 | Crown |\n"
    segments = parse_markdown_tables(two)
    assert [len(s.schema.labels) for s in segments] == [5, 2]


# -- the strategy -----------------------------------------------------------

async def test_reads_a_scanned_page_the_deterministic_readers_cannot(tmp_path):
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        sent["auth"] = request.headers.get("authorization")
        return ocr_response()

    path = scanned_pdf(tmp_path / "scan.pdf")
    table = await strategy(handler).extract(page_of(path))

    assert [r.code for r in table.rows] == ["D0120", "D0274"]
    assert table.strategy == "document_ai"
    assert sent["document"]["document_url"].startswith("data:application/pdf;base64,")
    assert sent["auth"] == "Bearer test-key"


async def test_rows_whose_code_is_not_on_a_readable_page_are_dropped(tmp_path):
    """On a page that does have text, the service's output is checked against it."""
    path = scanned_pdf(tmp_path / "scan.pdf")
    words = [Word(text="D0120", x0=0, x1=10, top=0, bottom=10, bold=False)]
    table = await strategy(lambda r: ocr_response()).extract(page_of(path, words=words))

    assert [r.code for r in table.rows] == ["D0120"]        # D0274 is not on this page


async def test_malformed_rows_are_discarded(tmp_path):
    bad = "| Code | Description |\n| --- | --- |\n| NOTACODE | something |\n| D0120 | fine |\n"
    path = scanned_pdf(tmp_path / "scan.pdf")
    table = await strategy(lambda r: ocr_response(bad)).extract(page_of(path))

    assert [r.code for r in table.rows] == ["D0120"]


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_errors_are_retried_then_raised(tmp_path, status):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(status, text="busy")

    path = scanned_pdf(tmp_path / "scan.pdf")
    with pytest.raises(LLMTransientError):
        await strategy(handler).extract(page_of(path))
    assert calls["n"] == Settings().max_llm_attempts


async def test_auth_failure_is_permanent(tmp_path):
    path = scanned_pdf(tmp_path / "scan.pdf")
    with pytest.raises(LLMPermanentError):
        await strategy(lambda r: httpx.Response(401, text="nope")).extract(page_of(path))


async def test_unexpected_response_shape_is_an_output_error(tmp_path):
    path = scanned_pdf(tmp_path / "scan.pdf")
    with pytest.raises(LLMOutputError):
        await strategy(lambda r: httpx.Response(200, json={"unexpected": True})).extract(page_of(path))


async def test_page_budget_caps_what_a_document_can_spend(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return ocr_response()

    path = scanned_pdf(tmp_path / "scan.pdf", pages=4)
    reader = strategy(handler, document_ai_max_pages=2)
    for number in (1, 2, 3, 4):
        await reader.extract(page_of(path, number=number))
    assert calls["n"] == 2

    reader.reset_budget()                                   # next document starts fresh
    await reader.extract(page_of(path))
    assert calls["n"] == 3


async def test_missing_api_key_fails_fast():
    with pytest.raises(LLMPermanentError):
        MistralDocumentAIStrategy(Settings(document_ai_provider="mistral", mistral_api_key=None))


# -- placement in the cascade ----------------------------------------------

async def test_fallback_is_not_called_when_a_deterministic_reader_works(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return ocr_response()

    ruled = PageView(number=1, width=612.0, height=792.0, words=[], text="D0120",
                     ruled_tables=[[["Code", "Description", "Frequency"],
                                    ["D0120", "Periodic oral evaluation", "2 per year"]]],
                     rects=[], source_path=scanned_pdf(tmp_path / "s.pdf"))
    cascade = TableCascade(fallbacks=[strategy(handler)])
    result = await cascade.extract_page(ruled)

    assert result.strategy == "ruled"
    assert calls["n"] == 0                                  # nothing was spent


async def test_a_failing_fallback_degrades_instead_of_killing_the_document(tmp_path):
    path = scanned_pdf(tmp_path / "scan.pdf")
    cascade = TableCascade(fallbacks=[strategy(lambda r: httpx.Response(401))])
    result = await cascade.extract_page(page_of(path))

    assert result.rows == []
    assert result.strategy in ("none", "ruled", "geometric")


async def test_scanned_document_is_rejected_without_a_fallback_and_read_with_one(tmp_path, settings):
    path = scanned_pdf(tmp_path / "scan.pdf")

    without = DentalGuidePipeline(settings, StubLLMClient())
    with pytest.raises(Exception) as exc:
        await without.run(path)
    assert "text layer" in str(exc.value)

    cascade = TableCascade(fallbacks=[strategy(lambda r: ocr_response())])
    with_fallback = DentalGuidePipeline(settings, StubLLMClient(), cascade=cascade)
    result = await with_fallback.run(path)

    assert [r.dental_code for r in result.rows] == ["D0120", "D0274"]
    assert result.rows[0].in_network == "100%"
    assert result.rows[0].strategy == "document_ai"
    assert any(f.rule == "dental_guide.rows_not_locally_verifiable"
               for f in result.validation.flags)
