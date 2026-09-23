from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.llm.scripted import ScriptedLLMClient
from app.adapters.llm.stub import StubLLMClient
from app.api.deps import get_service
from app.api.main import create_app
from app.application.service import ExtractionService
from app.application.dental_guide import DentalGuidePipeline
from app.application.pipeline import ExtractionPipeline
from app.adapters.repository.memory import InMemoryJobRepository
from app.config import Settings
from app.domain.models import JobStatus

ROOT = Path(__file__).resolve().parent.parent

SINGLE_PACKAGE = """115 2024 Evidence of Coverage for Test Plan (HMO)
HMO-MAPD 1234567TESTNMUB_0102_R Revised 10/06/2023 Customer Service 1-888-000-0000
Optional supplemental package 1 – Preventive dental package
Premium $13.00 monthly premium
Two cleanings per year
• D1110 – Prophylaxis – adult
The plan will pay up to $500 for preventive dental benefits each year (benefit maximum).
Coverage is available from LIBERTY Dental providers only.
You pay no copay for the preventive dental benefits listed.
Exclusions & Limitations:
• Services must be rendered by a contracted provider.
"""


def llm_reply(**overrides) -> dict:
    """A contract-valid reply for SINGLE_PACKAGE, with verbatim quotes."""
    reply = {
        "package_name": "Optional supplemental package 1 - Preventive dental package",
        "package_name_evidence": "Optional supplemental package 1 - Preventive dental package",
        "benefit_domains": ["dental"],
        "network_restriction": "LIBERTY Dental providers only",
        "network_restriction_evidence": "Coverage is available from LIBERTY Dental providers only",
        "services": [{
            "name": "Cleanings", "category": "preventive", "limit_count": 2,
            "limit_unit": "cleanings", "limit_period": "per_year",
            "limit_raw": "Two cleanings per year", "codes": ["D1110"],
            "evidence_quote": "Two cleanings per year",
        }],
        "cost_share": [{
            "type": "copay", "amount_usd": 0.0, "percent": None,
            "applies_to": "the preventive dental benefits listed",
            "evidence_quote": "You pay no copay for the preventive dental benefits listed.",
        }],
        "exclusions": [{
            "text": "Services must be rendered by a contracted provider.",
            "kind": "network_restriction",
            "evidence_quote": "Services must be rendered by a contracted provider.",
        }],
        "truncated": False,
    }
    reply.update(overrides)
    return reply


@pytest.fixture
def settings() -> Settings:
    return Settings(llm_provider="stub", block_concurrency=1, retry_base_delay_seconds=0.001,
                    retry_max_delay_seconds=0.002, json_logs=False)


@pytest.fixture
def sample_text() -> str:
    return (ROOT / "extracted_text.txt").read_text()


def make_service(settings: Settings, llm) -> ExtractionService:
    return ExtractionService(InMemoryJobRepository(), ExtractionPipeline(settings, llm), settings,
                             dental_guide=DentalGuidePipeline(settings, llm))


@pytest.fixture
def scripted():
    def _factory(script):
        return ScriptedLLMClient(script)
    return _factory


@pytest.fixture
def stub_service(settings) -> ExtractionService:
    return make_service(settings, StubLLMClient())


@pytest.fixture
def client(stub_service):
    app = create_app()
    app.dependency_overrides[get_service] = lambda: stub_service
    with TestClient(app) as c:
        yield c


async def run_to_completion(service: ExtractionService, text: str):
    job, _ = await service.submit(text)
    await service.drain()
    return await service.get(job.job_id)


def poll(client: TestClient, job_id: str, tries: int = 100):
    import time
    for _ in range(tries):
        body = client.get(f"/extract/{job_id}").json()
        if body["status"] in (JobStatus.succeeded, JobStatus.partial, JobStatus.failed):
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached a terminal state")
