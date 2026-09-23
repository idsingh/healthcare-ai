"""Guard: the extractor must not learn the sample documents by name.

The brief says the solution is validated against Dental Guides we have never
seen, so anything in app/ that keys off a carrier, plan or file name would be a
defect even if every provided guide still passed.
"""
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
FORBIDDEN = ("humana", "anthem", "wellcare", "centene", "liberty dental", "17_dg", "h0544",
             "dbd_copper", "den080", "y0020", "hmo-mapd", "medicare network")


def test_no_document_specific_identifiers_in_the_extraction_code():
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        text = path.read_text().lower()
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append(f"{path.relative_to(APP.parent)}: {needle}")
    assert offenders == [], f"document-specific identifiers found: {offenders}"


def test_column_labels_are_matched_by_vocabulary_not_by_position():
    from app.application.tables.mapping import Field, map_labels

    shuffled = map_labels(["Out-of-network coverage", "In-network coverage",
                           "Frequency/limitations", "Description of benefits", "ADA code"])
    assert shuffled == {Field.out_network: 0, Field.in_network: 1, Field.frequency: 2,
                        Field.description: 3, Field.code: 4}


def test_unseen_header_wording_still_maps():
    from app.application.tables.mapping import Field, map_labels

    assert map_labels(["CDT Code", "Nomenclature", "Benefit Limitations",
                       "Participating Provider", "Non-Participating Provider"]) == {
        Field.code: 0, Field.description: 1, Field.frequency: 2,
        Field.in_network: 3, Field.out_network: 4}


def test_code_shape_is_not_limited_to_the_codes_in_these_guides():
    from app.application.tables.models import find_code

    assert find_code("D9995 teledentistry") == "D9995"
    assert find_code("• D0708 - image capture") == "D0708"
    assert find_code("D1234A variant") == "D1234A"
    assert find_code("no code here") is None
