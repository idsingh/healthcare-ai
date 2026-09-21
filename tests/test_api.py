"""HTTP contract: the two endpoints, their status codes and error states."""
from __future__ import annotations

from tests.conftest import SINGLE_PACKAGE, poll


def test_submit_then_fetch(client, sample_text):
    submitted = client.post("/extract", json={"text": sample_text, "document_id": "eoc-excerpt"})
    assert submitted.status_code == 202
    body = submitted.json()
    assert body["status"] == "queued"
    assert body["links"]["self"] == f"/extract/{body['job_id']}"
    assert body["result"] is None

    final = poll(client, body["job_id"])
    assert final["status"] == "succeeded"
    assert final["needs_review"] is True                    # document is truncated
    assert len(final["result"]["packages"]) == 2
    assert final["result"]["run"]["content_sha256"] == body["content_sha256"]


def test_resubmitting_identical_text_replays_the_same_job(client):
    first = client.post("/extract", json={"text": SINGLE_PACKAGE})
    assert first.status_code == 202
    poll(client, first.json()["job_id"])

    second = client.post("/extract", json={"text": SINGLE_PACKAGE})
    assert second.status_code == 200                        # not 202: nothing new was started
    assert second.json()["idempotent_replay"] is True
    assert second.json()["job_id"] == first.json()["job_id"]


def test_unknown_job_returns_404_with_a_code(client):
    response = client.get("/extract/job_does_not_exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


def test_blank_and_short_input_are_rejected_before_any_work(client):
    for payload in ({"text": ""}, {"text": "   "}, {"text": "too short"}):
        response = client.post("/extract", json=payload)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"


def test_unknown_fields_are_rejected(client):
    response = client.post("/extract", json={"text": SINGLE_PACKAGE, "model": "gpt-4"})
    assert response.status_code == 422


def test_binary_input_fails_the_job_with_a_useful_error(client):
    response = client.post("/extract", json={"text": "\x00\x01\x02\x03" * 100})
    final = poll(client, response.json()["job_id"])
    assert final["status"] == "failed"
    assert final["error"]["code"] == "input_rejected"
    assert final["error"]["stage"] == "input_validation"
    assert final["result"] is None


def test_document_without_packages_completes_and_says_so(client):
    text = ("115 2024 Evidence of Coverage for Test Plan (HMO)\n"
            "This section describes how to file an appeal. " * 5)
    final = poll(client, client.post("/extract", json={"text": text}).json()["job_id"])
    assert final["status"] == "succeeded"
    assert final["result"]["packages"] == []
    assert any(f["rule"] == "segmentation.no_packages" for f in final["result"]["validation"]["flags"])
    assert final["needs_review"] is True


def test_request_id_is_echoed_for_correlation(client):
    response = client.post("/extract", json={"text": SINGLE_PACKAGE},
                           headers={"x-request-id": "trace-me-123"})
    assert response.headers["x-request-id"] == "trace-me-123"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "schema_version": "1.0.0"}
