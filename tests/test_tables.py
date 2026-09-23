"""Table layer: synthetic pages, no PDFs.

Each test encodes one layout hazard seen in real benefit guides, so a
regression shows up as a named failure rather than as fewer CSV rows.
"""
from __future__ import annotations

from app.adapters.pdf.pdfplumber_source import PageView, Rect, Word
from app.application.tables.cascade import TableCascade
from app.application.tables.geometric import GeometricTableStrategy
from app.application.tables.mapping import Field, map_labels, unmapped, unqualified_coverage
from app.application.tables.models import PageTable, TableSchema
from app.application.tables.ruled import RuledTableStrategy


def words(spec: list[tuple[str, float, float, float]]) -> list[Word]:
    """(text, x0, x1, top) -> Word, with a 10pt line height."""
    return [Word(text=t, x0=x0, x1=x1, top=top, bottom=top + 10, bold=False)
            for t, x0, x1, top in spec]


def page(word_list, *, rects=None, tables=None, width=600.0, height=800.0, number=1) -> PageView:
    return PageView(number=number, width=width, height=height, words=word_list,
                    text=" ".join(w.text for w in word_list), ruled_tables=tables or [],
                    rects=rects or [])


HEADER = [("Code", 20, 60, 100), ("Description", 100, 200, 100),
          ("Frequency/limitations", 300, 420, 100),
          ("In-network", 450, 510, 88), ("coverage", 450, 505, 100),
          ("Out-of-network", 530, 595, 88), ("coverage", 530, 585, 100)]


def test_geometric_reads_an_unruled_table():
    view = page(words(HEADER + [
        ("Exams", 20, 60, 130),
        ("Periodic oral evaluation", 100, 260, 150),
        ("D0120", 20, 60, 160), ("100%", 455, 490, 160), ("100%", 535, 570, 160),
        ("established patient", 100, 210, 170),
    ]))
    table = GeometricTableStrategy().extract(view)

    assert table.schema.labels == ["Code", "Description", "Frequency/limitations",
                                   "In-network coverage", "Out-of-network coverage"]
    assert len(table.rows) == 1
    row = table.rows[0]
    assert row.code == "D0120"
    assert row.cells[1] == "Periodic oral evaluation established patient"   # wrapped both ways
    assert row.cells[3] == "100%" and row.cells[4] == "100%"
    assert row.group == "Exams"


def test_wrapped_line_never_crosses_a_section_heading():
    """Without this, the last line of one group's description is pulled into
    the first row of the next group."""
    view = page(words(HEADER + [
        ("Exams", 20, 60, 130),
        ("D0120", 20, 60, 150), ("Periodic oral evaluation", 100, 260, 150),
        ("trailing detail", 100, 180, 170),
        ("Crowns", 20, 70, 200),
        ("D2710", 20, 60, 220), ("Crown resin", 100, 180, 220),
    ]))
    rows = GeometricTableStrategy().extract(view).rows

    assert [r.code for r in rows] == ["D0120", "D2710"]
    assert "trailing detail" in rows[0].cells[1]
    assert "trailing detail" not in rows[1].cells[1]
    assert rows[1].group == "Crowns"


def test_cell_spanning_several_rows_applies_to_all_of_them():
    """A frequency stated once for a block of codes is drawn as one tall
    rectangle; every row it covers carries that value."""
    view = page(
        words(HEADER + [
            ("D0120", 20, 60, 150), ("Periodic", 100, 150, 150), ("100%", 455, 490, 150),
            ("D0140", 20, 60, 180), ("Limited", 100, 150, 180), ("100%", 455, 490, 180),
            ("Unlimited", 305, 360, 160), ("up to annual maximum", 305, 415, 170),
        ]),
        rects=[Rect(x0=300, x1=430, top=140, bottom=200)])
    rows = GeometricTableStrategy().extract(view).rows

    assert len(rows) == 2
    assert all(r.cells[2] == "Unlimited up to annual maximum" for r in rows)


def test_page_furniture_is_not_a_benefit_group():
    view = page(words([
        ("HumanaDental", 20, 120, 10), ("Medicare", 130, 190, 10),        # running header
    ] + HEADER + [
        ("Exams", 20, 60, 130),
        ("D0120", 20, 60, 150), ("Periodic", 100, 150, 150),
    ] + [("4", 20, 30, 780), ("COP_DEN26", 35, 110, 780)]))               # footer
    rows = GeometricTableStrategy().extract(view).rows

    assert [r.group for r in rows] == ["Exams"]
    assert all("COP_DEN26" not in " ".join(r.cells) for r in rows)


def test_header_survives_a_continuation_page_that_repeats_only_part_of_it():
    strategy = GeometricTableStrategy()
    first = strategy.extract(page(words(HEADER + [
        ("D0120", 20, 60, 150), ("Periodic", 100, 150, 150), ("100%", 455, 490, 150),
        ("100%", 535, 570, 150)])))
    partial = [("Code", 20, 60, 100), ("Description", 100, 200, 100),
               ("Frequency/limitations", 300, 420, 100),
               ("coverage", 450, 505, 100), ("coverage", 530, 585, 100)]
    second = strategy.extract(page(words(partial + [
        ("D0140", 20, 60, 150), ("Limited", 100, 150, 150), ("100%", 455, 490, 150),
        ("100%", 535, 570, 150)]), number=2), first.schema)

    assert second.schema.labels[3] == "In-network coverage"
    assert Field.out_network in map_labels(second.schema.labels)


def test_ruled_strategy_reads_headings_and_rows():
    view = page([], tables=[[
        ["Code", "Code Description", "Periodicity"],
        ["Diagnostic (Preventive) Services", "", ""],
        ["D0120", "Periodic Oral Evaluation", "2 of (D0120) every plan year"],
    ]])
    table = RuledTableStrategy().extract(view)

    assert table.schema.labels == ["Code", "Code Description", "Periodicity"]
    assert len(table.rows) == 1
    assert table.rows[0].group == "Diagnostic (Preventive) Services"
    assert table.rows[0].cells[2].startswith("2 of (D0120)")


async def test_cascade_prefers_the_strategy_that_reads_the_page_better():
    good = page([], tables=[[
        ["Code", "Description", "Frequency"],
        ["D0120", "Periodic oral evaluation", "2 per year"]]])
    result = await TableCascade().extract_page(good)
    assert result.strategy == "ruled" and len(result.rows) == 1


async def test_cascade_survives_a_strategy_that_raises():
    class Exploding:
        name = "exploding"

        def extract(self, page, carried=None):
            raise RuntimeError("bad page")

    view = page([], tables=[[["Code", "Description", "Frequency"],
                             ["D0120", "Periodic oral evaluation", "2 per year"]]])
    cascade = TableCascade(strategies=[Exploding(), RuledTableStrategy()])
    assert len((await cascade.extract_page(view)).rows) == 1


async def test_cascade_falls_back_to_the_llm_reader_when_nothing_deterministic_works():
    class Fallback:
        name = "llm"

        def extract(self, page, carried=None):
            schema = TableSchema(columns=[], source="llm")
            from app.application.tables.models import RawRow
            return PageTable(schema=schema, rows=[RawRow(cells=["D0120", "Periodic oral evaluation"])],
                             strategy="llm")

    view = page(words([("D0120", 20, 60, 400)]))          # a code, but no readable table
    cascade = TableCascade(strategies=[], llm_strategy=Fallback())
    result = await cascade.extract_page(view)
    assert result.strategy == "llm" and len(result.rows) == 1


def test_column_mapping_handles_the_labels_these_guides_actually_use():
    assert map_labels(["ADA code", "Description of benefits", "Frequency/limitations",
                       "In-network coverage", "Out-of-network coverage"]) == {
        Field.code: 0, Field.description: 1, Field.frequency: 2,
        Field.in_network: 3, Field.out_network: 4}
    # 'Code Description' means description, not code
    assert map_labels(["Code", "Code Description", "Periodicity"]) == {
        Field.code: 0, Field.description: 1, Field.frequency: 2}
    assert map_labels(["Codes", "Description", "Prior Authorization Required?",
                       "Frequency Limitation", "Service Category"]) == {
        Field.code: 0, Field.description: 1, Field.frequency: 3, Field.group: 4}


def test_unqualified_coverage_is_detected_and_unknown_columns_are_reported():
    assert unqualified_coverage(["Code", "Description", "Coverage"])
    assert not unqualified_coverage(["Code", "In-network coverage", "Out-of-network coverage"])
    assert unmapped(["Codes", "Prior Authorization Required?"]) == ["Prior Authorization Required?"]
