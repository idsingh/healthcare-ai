"""End to end on the real excerpt, with the offline stub model.

These are the facts a reviewer would check by hand against extracted_text.txt.
"""
from __future__ import annotations

import pytest

from app.adapters.llm.stub import StubLLMClient
from app.application.preprocess import Preprocessor, collapse
from app.domain.models import FieldStatus, JobStatus, Severity
from tests.conftest import make_service, run_to_completion


@pytest.fixture
async def result(settings, sample_text):
    job = await run_to_completion(make_service(settings, StubLLMClient()), sample_text)
    assert job.status == JobStatus.succeeded
    return job.result


async def test_document_facts(result):
    assert result.document.plan_name.value == "Anthem Select (HMO)"
    assert result.document.plan_year.value == 2024
    assert result.document.source_pages == [115, 116]
    assert result.document.truncated is True               # excerpt ends mid-sentence
    assert result.document.issuer.status == FieldStatus.not_stated   # correct abstention


async def test_both_packages_with_premiums_maximums_and_network(result):
    one, two = result.packages
    assert one.name.value == "Optional supplemental package 1 - Preventive dental package"
    assert (one.premium.value.amount_usd, one.premium.value.cadence.value) == (13.0, "monthly")
    assert (one.benefit_maximum.value.amount_usd, one.benefit_maximum.value.period.value) == (500.0, "per_year")
    assert one.network_restriction.value == "LIBERTY Dental providers only"

    assert two.name.value == "Optional supplemental package 2 - Dental and vision package"
    assert two.premium.value.amount_usd == 32.0
    assert two.benefit_maximum.value.amount_usd == 1000.0
    assert two.network_restriction.value == "LIBERTY Dental providers only"
    assert set(two.benefit_domains) == {"dental", "vision"}


async def test_codes_span_the_page_break(result):
    """Package 1's X-ray codes continue onto page 116 after a repeated header."""
    codes = [c.code for c in result.packages[0].codes]
    assert len(codes) == 16
    assert codes[0] == "D0120" and "D0330" in codes and "D1208" in codes
    descriptions = {c.code: c.description for c in result.packages[0].codes}
    assert descriptions["D1110"] == "Prophylaxis - adult"
    assert descriptions["D0150"] == "Comprehensive oral evaluation - new or established patient"


async def test_services_and_limits(result):
    services = {s.name.lower(): s for s in result.packages[0].services}
    assert services["oral exams"].limit.count == 2.0
    assert services["cleanings"].limit.count == 2.0 and services["cleanings"].codes == ["D1110"]
    assert services["fluoride treatments"].codes == ["D1208"]


async def test_cost_share_attaches_to_the_right_package(result):
    one, two = result.packages
    assert [cs.type.value for cs in one.cost_share] == ["copay"]
    assert one.cost_share[0].amount_usd == 0.0
    assert one.cost_share[0].applies_to.service_ids                 # attached to services

    percents = sorted(cs.percent for cs in two.cost_share if cs.percent is not None)
    assert percents == [20.0, 50.0]
    assert two.cost_share[0].amount_usd == 0.0                      # $0 copay, package 2


async def test_truncation_is_reported_not_guessed(result):
    two = result.packages[1]
    truncated = [cs for cs in two.cost_share if cs.status == FieldStatus.truncated]
    assert len(truncated) == 1 and truncated[0].percent == 50.0
    assert two.needs_review is True
    assert any(f.rule == "document.truncated" for f in result.validation.flags)


async def test_exclusions_are_normalized_and_kinds_assigned(result):
    kinds = [e.kind.value for e in result.packages[0].exclusions]
    assert {"network_restriction", "member_liability", "service_excluded",
            "claims_process", "accumulator"} <= set(kinds)
    assert result.packages[1].exclusions == []
    assert any(f.rule == "invariants.exclusions_not_stated" for f in result.validation.flags)


async def test_every_evidence_quote_resolves_in_the_source(result, settings, sample_text):
    """Independent re-verification: re-normalize the file and check each quote."""
    doc = Preprocessor(settings).run(sample_text)
    checked = 0
    for pkg in result.packages:
        window = (pkg.source_span.start, pkg.source_span.end)
        for node in [pkg.name, pkg.premium, pkg.benefit_maximum, pkg.network_restriction,
                     *pkg.services, *pkg.codes, *pkg.cost_share, *pkg.exclusions]:
            if node.evidence is None:
                continue
            checked += 1
            assert doc.flat[node.evidence.start:node.evidence.end] == collapse(node.evidence.quote)
            assert window[0] <= node.evidence.start < window[1]
    assert checked >= 40
    assert result.validation.metrics.groundedness_rate == 1.0


async def test_no_error_flags_and_result_is_schema_stable(result):
    assert [f for f in result.validation.flags if f.severity == Severity.error] == []
    assert result.validation.passed is True
    dumped = result.model_dump(mode="json")
    assert dumped["schema_version"] == "1.0.0"
    assert dumped["run"]["prompt_version"] == "eoc-supplemental-v3"


async def test_same_input_produces_the_same_output(settings, sample_text):
    first = await run_to_completion(make_service(settings, StubLLMClient()), sample_text)
    second = await run_to_completion(make_service(settings, StubLLMClient()), sample_text)
    strip = lambda r: {k: v for k, v in r.model_dump(mode="json").items() if k != "run"}
    assert strip(first.result) == strip(second.result)
    assert first.result.run.idempotency_key == second.result.run.idempotency_key


async def test_blocks_are_processed_concurrently(settings, sample_text):
    import asyncio
    from app.adapters.llm.stub import StubLLMClient as Stub

    class SlowStub(Stub):
        def __init__(self):
            self.in_flight = 0
            self.max_in_flight = 0

        async def complete_json(self, **kwargs):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.05)
            try:
                return await Stub.complete_json(self, **kwargs)
            finally:
                self.in_flight -= 1

    llm = SlowStub()
    settings = settings.model_copy(update={"block_concurrency": 4})
    await run_to_completion(make_service(settings, llm), sample_text)
    assert llm.max_in_flight == 2                            # both packages at once


async def test_a_complete_sentence_at_a_block_boundary_is_not_truncated(result):
    """Package 1's last exclusion sits right against package 2's heading, but it
    ends with a full stop — it is complete, not cut off."""
    last = result.packages[0].exclusions[-1]
    assert last.text.endswith("out-of-pocket amount.")
    assert last.status == FieldStatus.found


async def test_exclusion_evidence_does_not_bleed_into_the_next_page(result):
    """Regression: the third exclusion's quote used to run past the page break
    and swallow '116 2024 Evidence of Coverage for Anthem Select (HMO) ...'."""
    texts = [e.text for e in result.packages[0].exclusions]
    assert texts[2] == ("Restorative dental (fillings) & endodontic, periodontic and "
                        "oral surgery services are excluded.")
    for exclusion in result.packages[0].exclusions:
        assert "Evidence of Coverage" not in exclusion.evidence.quote
        assert exclusion.evidence.quote.endswith(".")
