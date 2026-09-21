"""Preprocessing is where robustness to messy text is won or lost."""
from __future__ import annotations

import pytest

from app.application.preprocess import Preprocessor, collapse
from app.application.scanners import scan_block
from app.application.segmentation import Segmenter, classify_columns
from app.config import Settings
from app.domain.errors import InputRejected


def test_rejects_empty_short_and_binary_input(settings):
    pre = Preprocessor(settings)
    for bad in ("", "   \n\t ", "too short"):
        with pytest.raises(InputRejected):
            pre.run(bad)
    with pytest.raises(InputRejected) as exc:
        pre.run("\x00\x01\x02\x03" * 100)
    assert exc.value.code == "input_rejected"


def test_rejects_oversized_input():
    pre = Preprocessor(Settings(max_input_bytes=1000))
    with pytest.raises(InputRejected) as exc:
        pre.run("a" * 2000)
    assert exc.value.details["limit"] == 1000


def test_strips_running_headers_but_keeps_repeated_content(settings, sample_text):
    """The regression that matters: the same CDT bullets and the same
    'You pay no copay ...' sentence appear in both packages. A naive frequency
    filter deletes the second copy and silently empties package 2."""
    doc = Preprocessor(settings).run(sample_text)
    assert doc.stats["boilerplate_lines_dropped"] >= 1
    assert doc.flat.count("HMO-MAPD 1054638MUSENMUB_0102_R") == 1     # footer deduped
    assert doc.flat.count("D0120 - Periodic oral evaluation") == 2    # content kept twice
    assert doc.flat.count("You pay no copay for the preventive") == 2


def test_joins_lines_and_folds_dashes(settings, sample_text):
    doc = Preprocessor(settings).run(sample_text)
    assert "D0140 - Limited oral evaluation - problem focused" in doc.flat
    assert "\n" not in doc.flat and "  " not in doc.flat


def test_locate_prefers_the_requested_window(settings, sample_text):
    doc = Preprocessor(settings).run(sample_text)
    blocks = Segmenter().segment(doc)
    pkg1, pkg2 = [b for b in blocks if b.kind == "package"]
    quote = "Coverage is available from LIBERTY Dental providers only"
    in_one = doc.locate(quote, within=pkg1.span)
    in_two = doc.locate(quote, within=pkg2.span)
    assert in_one and in_two and in_one != in_two
    assert pkg2.start <= in_two[0] < pkg2.end


def test_pages_and_package_boundaries(settings, sample_text):
    doc = Preprocessor(settings).run(sample_text)
    blocks = Segmenter().segment(doc)
    kinds = [b.kind for b in blocks]
    assert kinds == ["header", "package", "package"]
    assert blocks[1].pages == (115, 116)          # package 1 spans the page break
    assert doc.page_at(blocks[2].start) == 116


def test_column_classifier_separates_the_two_table_columns(settings, sample_text):
    doc = Preprocessor(settings).run(sample_text)
    pkg1 = [b for b in Segmenter().segment(doc) if b.kind == "package"][0]
    cols = classify_columns(pkg1)
    assert any("plan will pay up to $500" in ln for ln in cols["cost"])
    assert any("D0120" in ln for ln in cols["benefit"])


def test_scanners_read_every_number_on_both_packages(settings, sample_text):
    doc = Preprocessor(settings).run(sample_text)
    packages = [b for b in Segmenter().segment(doc) if b.kind == "package"]
    found = {b.block_id: {c.kind: c.value for c in scan_block(b)} for b in packages}
    assert found["pkg_1"]["premium"]["amount_usd"] == 13.0
    assert found["pkg_1"]["benefit_maximum"]["amount_usd"] == 500.0
    assert found["pkg_2"]["premium"]["amount_usd"] == 32.0
    assert found["pkg_2"]["benefit_maximum"]["amount_usd"] == 1000.0
    codes = [c.value["code"] for c in scan_block(packages[0]) if c.kind == "code"]
    assert codes[:3] == ["D0120", "D0140", "D0150"] and "D1208" in codes


def test_money_phrase_is_not_mistaken_for_a_frequency_limit(settings, sample_text):
    doc = Preprocessor(settings).run(sample_text)
    pkg1 = [b for b in Segmenter().segment(doc) if b.kind == "package"][0]
    limits = [c.value for c in scan_block(pkg1) if c.kind == "limit"]
    assert all(l["count"] != 500 for l in limits)
    assert {"oral exams", "cleanings", "fluoride treatments"} <= {l["unit"] for l in limits}


def test_collapse_is_idempotent():
    messy = "Two  oral\n exams   each\tyear"
    assert collapse(collapse(messy)) == collapse(messy) == "Two oral exams each year"


def test_running_header_is_stripped_even_when_its_first_copy_is_mid_page(settings, sample_text):
    """Page 116 repeats the table header that sits mid-page on 115 (the excerpt
    starts partway through that page). If it survives, it leaks into the
    evidence quote of the exclusion that precedes it."""
    doc = Preprocessor(settings).run(sample_text)
    assert doc.flat.count("Optional supplemental benefits What you must pay") == 1
    assert doc.flat.count("116 2024 Evidence of Coverage") == 0
