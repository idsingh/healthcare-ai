"""The validators, driven directly: each rule must bite on a tampered result."""
from __future__ import annotations

import pytest

from app.application.preprocess import Preprocessor
from app.application.validation import ValidationPipeline
from app.domain.models import (
    AppliesTo, CostShare, CostShareType, FieldStatus, Severity,
)
from tests.conftest import SINGLE_PACKAGE, make_service, run_to_completion
from app.adapters.llm.stub import StubLLMClient


@pytest.fixture
async def extracted(settings):
    job = await run_to_completion(make_service(settings, StubLLMClient()), SINGLE_PACKAGE)
    doc = Preprocessor(settings).run(SINGLE_PACKAGE)
    return job.result, doc


def rules_fired(result, severity=Severity.error) -> set[str]:
    return {f.rule for f in result.validation.flags if f.severity == severity}


async def test_clean_result_passes(extracted):
    result, _ = extracted
    assert result.validation.passed
    assert result.validation.metrics.groundedness_rate == 1.0
    assert rules_fired(result) == set()


async def test_fabricated_evidence_nulls_the_value(extracted):
    result, doc = extracted
    result.packages[0].benefit_maximum.evidence.quote = "The plan will pay up to $1,200 each year"

    ValidationPipeline().run(result, doc)

    assert "groundedness.not_in_source" in rules_fired(result)
    assert result.packages[0].benefit_maximum.value is None
    assert result.packages[0].benefit_maximum.status == FieldStatus.unverified
    assert result.validation.passed is False


async def test_number_that_is_not_in_its_quote_is_an_error(extracted):
    result, doc = extracted
    result.packages[0].premium.value.amount_usd = 130.0     # quote still says $13.00

    ValidationPipeline().run(result, doc)

    assert "numeric.not_in_evidence" in rules_fired(result)
    assert result.validation.passed is False


async def test_missing_premium_is_an_error_not_a_null(extracted):
    result, doc = extracted
    result.packages[0].premium.status = FieldStatus.not_stated
    result.packages[0].premium.value = None
    result.packages[0].premium.evidence = None

    ValidationPipeline().run(result, doc)
    assert "invariants.premium_missing" in rules_fired(result)


async def test_unknown_code_and_unknown_service_are_caught(extracted):
    result, doc = extracted
    pkg = result.packages[0]
    pkg.services[0].codes.append("D9999")
    pkg.cost_share[0].applies_to.service_ids.append("pkg_1.svc_ghost")

    ValidationPipeline().run(result, doc)

    assert {"references.unknown_code", "references.unknown_service"} <= rules_fired(result)


async def test_copay_and_coinsurance_on_the_same_service_contradict(extracted):
    result, doc = extracted
    pkg = result.packages[0]
    service_id = pkg.services[0].service_id
    pkg.cost_share[0].applies_to = AppliesTo(description="cleanings", service_ids=[service_id])
    pkg.cost_share.append(CostShare(
        cost_share_id="pkg_1.cs_2", type=CostShareType.coinsurance, percent=20.0,
        applies_to=AppliesTo(description="cleanings", service_ids=[service_id]),
        evidence=pkg.cost_share[0].evidence))

    ValidationPipeline().run(result, doc)
    assert "invariants.conflicting_cost_share" in rules_fired(result)


async def test_disjoint_service_sets_do_not_contradict(extracted):
    """$0 copay on preventive plus 20% on restorative is legal, and common."""
    result, doc = extracted
    pkg = result.packages[0]
    pkg.cost_share.append(CostShare(
        cost_share_id="pkg_1.cs_2", type=CostShareType.coinsurance, percent=20.0,
        applies_to=AppliesTo(description="restorative", service_ids=[]),
        evidence=pkg.cost_share[0].evidence))

    ValidationPipeline().run(result, doc)
    assert "invariants.conflicting_cost_share" not in rules_fired(result)


async def test_low_certainty_routes_to_review(extracted):
    result, doc = extracted
    result.packages[0].services[0].status = FieldStatus.ambiguous

    ValidationPipeline().run(result, doc)

    assert result.packages[0].needs_review
    assert result.validation.needs_review
    assert "review.low_certainty" in rules_fired(result, Severity.warn)
