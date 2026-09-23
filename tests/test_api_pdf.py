"""The PDF path over HTTP: upload, poll, download CSV, and the error states."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.application.csv_export import COLUMNS
from tests.conftest import SINGLE_PACKAGE, poll

GUIDES = Path(__file__).resolve().parent.parent / "data" / "dental_guides"
PDFS = sorted(GUIDES.glob("*.pdf"))
needs_pdfs = pytest.mark.skipif(not PDFS, reason="dental guide PDFs are not present")


@needs_pdfs
def test_upload_pdf_then_fetch_rows_and_csv(client):
    with PDFS[0].open("rb") as handle:
        response = client.post("/extract/upload", files={"file": (PDFS[0].name, handle, "application/pdf")})
    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == "dental_guide"

    final = poll(client, body["job_id"])
    assert final["status"] == "succeeded"
    assert final["row_count"] > 50
    assert final["links"]["csv"] == f"/extract/{body['job_id']}?format=csv"
    assert final["result"]["document"]["file_name"] == PDFS[0].name

    csv_response = client.get(f"/extract/{body['job_id']}?format=csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    lines = csv_response.text.strip().splitlines()
    assert lines[0] == ",".join(COLUMNS)
    assert len(lines) - 1 == final["row_count"]


@needs_pdfs
def test_uploading_the_same_pdf_twice_replays_the_job(client):
    data = PDFS[0].read_bytes()
    first = client.post("/extract/upload", files={"file": (PDFS[0].name, data, "application/pdf")})
    poll(client, first.json()["job_id"])
    second = client.post("/extract/upload", files={"file": (PDFS[0].name, data, "application/pdf")})

    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.json()["job_id"] == first.json()["job_id"]


def test_uploading_something_that_is_not_a_pdf_is_rejected(client):
    response = client.post("/extract/upload",
                           files={"file": ("notes.txt", b"just some text", "text/plain")})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "input_rejected"


def test_csv_is_refused_for_a_text_job_with_a_clear_reason(client):
    job_id = client.post("/extract", json={"text": SINGLE_PACKAGE}).json()["job_id"]
    poll(client, job_id)
    response = client.get(f"/extract/{job_id}?format=csv")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_projection"


def test_unknown_format_is_rejected(client):
    response = client.get("/extract/job_whatever?format=xml")
    assert response.status_code == 422
