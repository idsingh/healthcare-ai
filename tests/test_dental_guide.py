"""Dental Guide extraction end to end, over the three real guides.

They have three different layouts (unruled Humana table, ruled WellCare table,
ruled Anthem table with a category column), which is the point: the same code
path reads all three, and the assertions are about behaviour, not about any
particular document.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

from app.adapters.llm.stub import StubLLMClient
from app.application.csv_export import COLUMNS, to_csv_string, to_record, write_csv
from app.application.dental_guide import DentalGuidePipeline
from app.domain.errors import InputRejected
from app.domain.models import BenefitRow, GroupSource

GUIDES = Path(__file__).resolve().parent.parent / "data" / "dental_guides"
SAMPLE = GUIDES / "Sample_output.csv"
pdfs = sorted(GUIDES.glob("*.pdf"))
needs_pdfs = pytest.mark.skipif(not pdfs, reason="dental guide PDFs are not present")


def norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower().replace("–", "-")


@pytest.fixture
def pipeline(settings):
    return DentalGuidePipeline(settings, StubLLMClient())


@needs_pdfs
@pytest.mark.parametrize("pdf", pdfs, ids=lambda p: p.stem[:18])
async def test_every_guide_yields_rows_whatever_its_layout(pipeline, pdf):
    result = await pipeline.run(pdf)

    assert len(result.rows) > 50
    assert result.validation.passed
    assert "code" in result.document.mapped_fields
    assert "description" in result.document.mapped_fields
    assert result.document.pages_with_rows > 1
    assert all(re.fullmatch(r"[A-Z]\d{4}[A-Z]?", r.dental_code) for r in result.rows)


@needs_pdfs
async def test_unruled_table_matches_the_customer_sample(pipeline):
    """The Humana guide draws its table without ruling lines; the sample output
    the customer supplied was produced from it."""
    result = await pipeline.run(GUIDES / "17_DG.pdf")
    rows = {r.dental_code: to_record(r) for r in result.rows}
    sample = list(csv.DictReader(SAMPLE.open()))

    assert all(s["Dental Code"] in rows for s in sample)
    matches = {column: sum(1 for s in sample if norm(rows[s["Dental Code"]][column]) == norm(s[column]))
               for column in COLUMNS}
    assert matches["Dental Code"] == len(sample)
    assert matches["Description"] >= 27
    assert matches["In-Network Coverage"] == len(sample)
    assert matches["Out-of-Network Coverage"] == len(sample)
    assert matches["Benefit Group"] >= 24
    # Frequency intentionally differs: see output/dental_guides/README.md — the
    # sample fills unstated frequencies with a default phrase, we read the PDF's
    # own merged cells and write "-" when nothing is stated.
    assert matches["Frequency/Limitations"] >= 16


@needs_pdfs
async def test_merged_frequency_cell_applies_to_every_row_it_covers(pipeline):
    result = await pipeline.run(GUIDES / "17_DG.pdf")
    rows = {r.dental_code: r for r in result.rows}
    for code in ("D0120", "D0140", "D0150", "D0160"):
        assert rows[code].frequency == "Unlimited up to annual maximum"


@needs_pdfs
async def test_guide_without_coverage_columns_reports_it_instead_of_inventing(pipeline):
    result = await pipeline.run(GUIDES / "DBD_Copper_Y0020_WCM_4952801E_C_R.pdf")

    assert "in_network" not in result.document.mapped_fields
    assert all(r.in_network is None and r.out_network is None for r in result.rows)
    assert any(f.rule == "dental_guide.column_not_present" for f in result.validation.flags)
    records = [to_record(r) for r in result.rows]
    assert all(rec["In-Network Coverage"] == "-" for rec in records)
    assert all(rec["Frequency/Limitations"] != "-" for rec in records[:5])


@needs_pdfs
async def test_explicit_category_column_wins_over_model_naming(pipeline):
    result = await pipeline.run(GUIDES / "H0544-056-000_dental_filtered__type1_pages.pdf")

    assert "group" in result.document.mapped_fields
    assert all(r.group_source is GroupSource.column for r in result.rows)
    assert result.rows[0].benefit_group == "Oral Exam"
    assert "Prior Authorization Required?" in result.document.unmapped_columns


@needs_pdfs
async def test_extraction_is_deterministic_for_the_same_input(pipeline):
    first = await pipeline.run(pdfs[0])
    second = await pipeline.run(pdfs[0])
    assert [to_record(r) for r in first.rows] == [to_record(r) for r in second.rows]
    assert first.run.content_sha256 == second.run.content_sha256


async def test_missing_file_is_rejected_before_any_work(pipeline):
    with pytest.raises(InputRejected):
        await pipeline.run("data/dental_guides/does_not_exist.pdf")


async def test_non_pdf_bytes_are_rejected(pipeline, tmp_path):
    fake = tmp_path / "not_a.pdf"
    fake.write_bytes(b"this is not a pdf at all")
    with pytest.raises(InputRejected):
        await pipeline.run(fake)


def test_csv_uses_the_customer_columns_and_marks_missing_values(tmp_path):
    rows = [BenefitRow(dental_code="D0120", description="Periodic oral  evaluation",
                       benefit_group="Exams", in_network="100%"),
            BenefitRow(dental_code="D9999")]
    written = write_csv(rows, tmp_path / "out.csv")
    text = (tmp_path / "out.csv").read_text()

    assert written == 2
    assert text.splitlines()[0] == ",".join(COLUMNS)
    first, second = list(csv.DictReader((tmp_path / "out.csv").open()))
    assert first["Description"] == "Periodic oral evaluation"        # whitespace collapsed
    assert first["Frequency/Limitations"] == "-" and first["Out-of-Network Coverage"] == "-"
    assert all(value == "-" for key, value in second.items() if key != "Dental Code")


def test_csv_placeholder_is_configurable():
    rows = [BenefitRow(dental_code="D0120")]
    assert ",N/A,N/A" in to_csv_string(rows, missing="N/A")
