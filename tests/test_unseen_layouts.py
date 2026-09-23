"""Layouts the extractor has never seen, generated on the fly.

The brief says the solution is validated against Dental Guides that are not in
the inputs, so the interesting question is not "does it read these three PDFs"
but "what does it do with a layout nobody anticipated". Each test below builds a
PDF with a different hazard and asserts the outcome — including the two outcomes
that are allowed to be imperfect: degrade with a flag, or refuse with a reason.
"""
from __future__ import annotations

import pytest

from app.adapters.llm.stub import StubLLMClient
from app.application.csv_export import to_record
from app.application.dental_guide import DentalGuidePipeline
from app.domain.errors import InputRejected

reportlab = pytest.importorskip("reportlab", reason="reportlab is needed to synthesise PDFs")

from reportlab.lib import colors                                    # noqa: E402
from reportlab.lib.pagesizes import A4, landscape, letter           # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet                # noqa: E402
from reportlab.pdfgen import canvas                                 # noqa: E402
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # noqa: E402

ROWS = [
    ("D0120", "Periodic oral evaluation", "2 per calendar year", "100%", "80%"),
    ("D0140", "Limited oral evaluation, problem focused", "2 per calendar year", "100%", "80%"),
    ("D0274", "Bitewings, four radiographic images", "1 every 12 months", "100%", "80%"),
    ("D1110", "Prophylaxis, adult", "2 per calendar year", "100%", "80%"),
    ("D2140", "Amalgam, one surface", "Once per tooth per 24 months", "80%", "50%"),
    ("D2740", "Crown, porcelain/ceramic", "1 per tooth every 5 years", "50%", "50%"),
    ("D7140", "Extraction, erupted tooth", "As needed", "80%", "50%"),
]
CODES = {r[0] for r in ROWS}


def ruled_pdf(path, header, rows, pagesize=letter):
    doc = SimpleDocTemplate(str(path), pagesize=pagesize)
    table = Table([list(header)] + [list(r) for r in rows], repeatRows=1)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                               ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    doc.build([Paragraph("Dental Benefit Summary", getSampleStyleSheet()["Title"]),
               Spacer(1, 12), table])
    return path


async def extract(settings, path):
    return await DentalGuidePipeline(settings, StubLLMClient()).run(path)


def records(result):
    return [to_record(r) for r in result.rows]


def complete(result) -> bool:
    """Every column filled for every row — no '-' anywhere."""
    return all(value != "-" for rec in records(result) for value in rec.values())


async def test_columns_in_a_different_order_with_unseen_header_wording(settings, tmp_path):
    path = ruled_pdf(tmp_path / "a.pdf",
                     ["Nomenclature", "CDT Code", "Benefit Limitations",
                      "Participating Provider", "Non-Participating Provider"],
                     [(r[1], r[0], r[2], r[3], r[4]) for r in ROWS])
    result = await extract(settings, path)

    assert {r.dental_code for r in result.rows} == CODES
    assert complete(result)
    assert records(result)[0]["Out-of-Network Coverage"] == "80%"


async def test_dollar_copays_instead_of_percentages(settings, tmp_path):
    path = ruled_pdf(tmp_path / "b.pdf",
                     ["Code", "Service", "Frequency", "You Pay (In-Network)", "You Pay (Out-of-Network)"],
                     [(r[0], r[1], r[2], "$0 copay", "$45 copay") for r in ROWS])
    result = await extract(settings, path)

    assert complete(result)
    assert records(result)[0]["In-Network Coverage"] == "$0 copay"


async def test_no_header_row_at_all(settings, tmp_path):
    """Columns are then identified by what they contain, not by their label."""
    path = ruled_pdf(tmp_path / "c.pdf", ROWS[0], ROWS[1:])
    result = await extract(settings, path)

    assert {r.dental_code for r in result.rows} >= CODES - {ROWS[0][0]}
    assert complete(result)


async def test_landscape_page_with_columns_we_do_not_need(settings, tmp_path):
    path = ruled_pdf(tmp_path / "d.pdf",
                     ["Tier", "Codes", "Description", "Prior Auth", "Frequency Limitation",
                      "In-Network Coverage", "Out-of-Network Coverage", "Notes"],
                     [("Tier 1", r[0], r[1], "No", r[2], r[3], r[4], "-") for r in ROWS],
                     pagesize=landscape(letter))
    result = await extract(settings, path)

    assert complete(result)
    assert set(result.document.unmapped_columns) >= {"Tier", "Notes"}


async def test_unruled_table_with_wording_nobody_anticipated(settings, tmp_path):
    """No ruling lines, headers no synonym list would contain ('Procedure',
    'What it covers', 'How often', 'In-plan', 'Out-of-plan'), wrapped cells."""
    path = tmp_path / "e.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    c.setFont("Helvetica-Bold", 9)
    for text, x, y in (("Procedure", 40, 60), ("What it covers", 150, 60), ("How often", 330, 60),
                       ("In-plan", 450, 60), ("Member", 450, 72), ("Out-of-plan", 520, 60)):
        c.drawString(x, height - y, text)
    y = height - 100
    c.setFont("Helvetica", 8)
    for group, rows in (("Preventive care", ROWS[:4]), ("Restorative care", ROWS[4:])):
        c.drawString(40, y, group)
        y -= 16
        for code, description, frequency, in_net, out_net in rows:
            head, _, tail = description.partition(", ")
            c.drawString(150, y, head)
            c.drawString(40, y - 10, code)
            c.drawString(330, y - 10, frequency)
            c.drawString(455, y - 10, in_net)
            c.drawString(530, y - 10, out_net)
            if tail:
                y -= 10
                c.drawString(150, y, tail)
            y -= 26
    c.save()
    result = await extract(settings, path)
    assert {r.dental_code for r in result.rows} == CODES
    assert complete(result)

    # With model naming switched off, the section headings are what survives —
    # they are the deterministic fallback for the benefit group.
    plain = await DentalGuidePipeline(
        settings.model_copy(update={"dg_llm_grouping": False}), StubLLMClient()).run(path)
    assert {r.benefit_group for r in plain.rows} == {"Preventive care", "Restorative care"}


async def test_two_tables_with_different_shapes_on_one_page(settings, tmp_path):
    """Each table gets its own column map; the second header must not be applied
    to the first table's rows."""
    path = tmp_path / "f.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=letter)
    styles = getSampleStyleSheet()
    first = Table([["Code", "Description", "Frequency"]] + [[r[0], r[1], r[2]] for r in ROWS[:3]])
    second = Table([["Dental Code", "Benefit", "In-network", "Out-of-network"]] +
                   [[r[0], r[1], r[3], r[4]] for r in ROWS[3:]])
    for table in (first, second):
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                   ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    doc.build([Paragraph("Preventive", styles["Heading2"]), first, Spacer(1, 24),
               Paragraph("Major services", styles["Heading2"]), second])
    result = await extract(settings, path)
    by_code = {r.dental_code: r for r in result.rows}

    assert {r.dental_code for r in result.rows} == CODES
    assert by_code["D0120"].frequency == "2 per calendar year"     # first table
    assert by_code["D0120"].in_network is None                     # first table has no coverage
    assert by_code["D2740"].in_network == "50%"                    # second table
    assert by_code["D2740"].description == "Crown, porcelain/ceramic"


async def test_scanned_pdf_is_refused_with_a_reason_not_an_empty_csv(settings, tmp_path):
    path = tmp_path / "g.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.rect(100, 400, 400, 200, fill=1)
    c.save()

    with pytest.raises(InputRejected) as exc:
        await extract(settings, path)
    assert "text layer" in exc.value.message and "OCR" in exc.value.message


async def test_a_guide_with_nothing_to_extract_fails_loudly(settings, tmp_path):
    path = tmp_path / "h.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 11)
    c.drawString(72, 700, "This booklet explains how to appeal a denied claim.")
    c.drawString(72, 680, "Call member services for details about your plan.")
    c.save()
    result = await extract(settings, path)

    assert result.rows == []
    assert not result.validation.passed
    assert any(f.rule == "dental_guide.no_rows" for f in result.validation.flags)
