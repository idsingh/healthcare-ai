"""Stage 5 — validation. The last gate before anything is called a result.

Five rules, each an independent object (OCP): adding a check means appending to
RULES. Severity contract: `error` nulls the value and fails the document,
`warn` flags it for review, `info` is telemetry only.
"""
from __future__ import annotations

import re
from typing import Iterator, Protocol

from app.application.preprocess import NormalizedDocument, collapse
from app.domain.models import (
    Code, CostShare, CostShareType, Exclusion, ExtractionResult, Field, FieldStatus, Flag, Package,
    Service, Severity, ValidationMetrics,
)

MONEY_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
REVIEW_STATES = {FieldStatus.ambiguous, FieldStatus.unverified, FieldStatus.truncated}
LOW_CONFIDENCE = 0.7

EvidenceNode = Field | Service | CostShare | Exclusion | Code


def walk(result: ExtractionResult) -> Iterator[tuple[str, EvidenceNode, Package | None]]:
    """Every node that carries evidence, with its JSON Pointer and package."""
    for name in ("plan_name", "plan_year", "issuer", "document_type", "form_id"):
        yield f"/document/{name}", getattr(result.document, name), None
    for i, pkg in enumerate(result.packages):
        base = f"/packages/{i}"
        for name in ("name", "premium", "benefit_maximum", "network_restriction"):
            yield f"{base}/{name}", getattr(pkg, name), pkg
        for collection in ("services", "codes", "cost_share", "exclusions"):
            for j, item in enumerate(getattr(pkg, collection)):
                yield f"{base}/{collection}/{j}", item, pkg


class Rule(Protocol):
    name: str

    def check(self, result: ExtractionResult, doc: NormalizedDocument) -> list[Flag]: ...


class GroundednessRule:
    """1. No value without verifiable evidence. The anti-hallucination backstop:
    a quote that is not in the source nulls its own value."""
    name = "groundedness"

    def check(self, result: ExtractionResult, doc: NormalizedDocument) -> list[Flag]:
        flags: list[Flag] = []
        for path, node, pkg in walk(result):
            ev = getattr(node, "evidence", None)
            if ev is None:
                if getattr(node, "status", None) not in (FieldStatus.not_stated, FieldStatus.unverified, None):
                    flags.append(Flag(rule=f"{self.name}.missing_evidence", severity=Severity.error,
                                      message=f"status={node.status} but no evidence attached", path=path))
                continue
            window = (pkg.source_span.start, pkg.source_span.end) if pkg and pkg.source_span.start is not None else None
            located = doc.locate(ev.quote, within=window)
            if not located:
                flags.append(Flag(rule=f"{self.name}.not_in_source", severity=Severity.error,
                                  message=f"evidence not found in source: {ev.quote[:60]!r}", path=path))
                self._withhold(node)
                continue
            if (ev.start, ev.end) != located:
                flags.append(Flag(rule=f"{self.name}.offset_mismatch", severity=Severity.warn,
                                  message="evidence offsets did not resolve to the quote; corrected", path=path))
                ev.start, ev.end = located
            if window and not (window[0] <= located[0] < window[1]):
                flags.append(Flag(rule=f"{self.name}.out_of_package_span", severity=Severity.warn,
                                  message="evidence resolves outside its own package span", path=path))
        return flags

    @staticmethod
    def _withhold(node: EvidenceNode) -> None:
        if hasattr(node, "status"):
            node.status = FieldStatus.unverified
        if isinstance(node, Field):
            node.value = None
        if isinstance(node, CostShare):
            node.amount_usd = node.percent = None


class NumericFidelityRule:
    """2. Every number must appear literally in its own evidence quote."""
    name = "numeric"

    def check(self, result: ExtractionResult, doc: NormalizedDocument) -> list[Flag]:
        flags: list[Flag] = []
        for path, node, _ in walk(result):
            ev = getattr(node, "evidence", None)
            if ev is None:
                continue
            quote = collapse(ev.quote)
            money = {float(m.replace(",", "")) for m in MONEY_RE.findall(quote)}
            if re.search(r"\bno copay\b", quote, re.I):
                money.add(0.0)
            percents = {float(p) for p in PCT_RE.findall(quote)}
            for kind, value in self._claims(node):
                pool = money if kind == "money" else percents
                if value not in pool:
                    flags.append(Flag(rule=f"{self.name}.not_in_evidence", severity=Severity.error,
                                      message=f"{kind} {value} is not present in its evidence quote", path=path))
        return flags

    @staticmethod
    def _claims(node: EvidenceNode) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        holders = [node]
        if isinstance(node, Field) and node.value is not None and hasattr(node.value, "amount_usd"):
            holders.append(node.value)
        for holder in holders:
            amount = getattr(holder, "amount_usd", None)
            percent = getattr(holder, "percent", None)
            if isinstance(amount, (int, float)):
                out.append(("money", float(amount)))
            if isinstance(percent, (int, float)):
                out.append(("percent", float(percent)))
        return out


class InvariantsRule:
    """3. Completeness and contradiction checks per package."""
    name = "invariants"

    def check(self, result: ExtractionResult, doc: NormalizedDocument) -> list[Flag]:
        flags: list[Flag] = []
        for i, pkg in enumerate(result.packages):
            base = f"/packages/{i}"
            if pkg.premium.status == FieldStatus.not_stated:
                flags.append(Flag(rule=f"{self.name}.premium_missing", severity=Severity.error,
                                  message=f"{pkg.package_id} is an optional supplemental package "
                                          "with no premium stated", path=f"{base}/premium"))
            if not pkg.exclusions:
                flags.append(Flag(rule=f"{self.name}.exclusions_not_stated", severity=Severity.warn,
                                  message=f"no exclusions observed for {pkg.package_id}; an empty list "
                                          "is not an assertion that none apply", path=f"{base}/exclusions"))
            by_service: dict[str, set[CostShareType]] = {}
            for j, cs in enumerate(pkg.cost_share):
                if cs.type == CostShareType.coinsurance and cs.percent is None and cs.status == FieldStatus.found:
                    flags.append(Flag(rule=f"{self.name}.coinsurance_without_percent", severity=Severity.error,
                                      message="coinsurance with no percent", path=f"{base}/cost_share/{j}"))
                if cs.type == CostShareType.copay and cs.amount_usd is None and cs.status == FieldStatus.found:
                    flags.append(Flag(rule=f"{self.name}.copay_without_amount", severity=Severity.error,
                                      message="copay with no amount", path=f"{base}/cost_share/{j}"))
                for sid in cs.applies_to.service_ids:
                    by_service.setdefault(sid, set()).add(cs.type)
            for sid, types in by_service.items():
                if {CostShareType.copay, CostShareType.coinsurance} <= types:
                    flags.append(Flag(rule=f"{self.name}.conflicting_cost_share", severity=Severity.error,
                                      message=f"{sid} carries both a copay and a coinsurance", path=base))
        return flags


class ReferentialIntegrityRule:
    """4. Codes and service references must resolve inside the package."""
    name = "references"

    def check(self, result: ExtractionResult, doc: NormalizedDocument) -> list[Flag]:
        flags: list[Flag] = []
        for i, pkg in enumerate(result.packages):
            declared = {c.code for c in pkg.codes}
            known = {s.service_id for s in pkg.services}
            for j, svc in enumerate(pkg.services):
                for code in svc.codes:
                    if code not in declared:
                        flags.append(Flag(rule=f"{self.name}.unknown_code", severity=Severity.error,
                                          message=f"{code} is not in {pkg.package_id}'s code list",
                                          path=f"/packages/{i}/services/{j}"))
            for j, cs in enumerate(pkg.cost_share):
                for sid in cs.applies_to.service_ids:
                    if sid not in known:
                        flags.append(Flag(rule=f"{self.name}.unknown_service", severity=Severity.error,
                                          message=f"{sid} does not exist",
                                          path=f"/packages/{i}/cost_share/{j}"))
            for j, code in enumerate(pkg.codes):
                if not code.description:
                    flags.append(Flag(rule=f"{self.name}.code_without_description", severity=Severity.warn,
                                      message=f"{code.code} has no description",
                                      path=f"/packages/{i}/codes/{j}"))
        return flags


class ReviewRule:
    """5. Surface everything uncertain; humans review the flagged minority."""
    name = "review"

    def check(self, result: ExtractionResult, doc: NormalizedDocument) -> list[Flag]:
        flags: list[Flag] = []
        for i, pkg in enumerate(result.packages):
            needs = pkg.degraded
            if pkg.degraded:
                flags.append(Flag(rule=f"{self.name}.degraded", severity=Severity.warn,
                                  message=f"{pkg.package_id} is deterministic-only: the LLM pass failed",
                                  path=f"/packages/{i}"))
            for path, node, node_pkg in walk(result):
                if node_pkg is not pkg:
                    continue
                status = getattr(node, "status", None)
                confidence = getattr(node, "confidence", None)
                if status in REVIEW_STATES or (isinstance(confidence, float) and confidence < LOW_CONFIDENCE):
                    needs = True
                    flags.append(Flag(rule=f"{self.name}.low_certainty",
                                      severity=Severity.warn if status in REVIEW_STATES else Severity.info,
                                      message=f"status={status} confidence={confidence}", path=path))
            pkg.needs_review = needs
        if result.document.truncated:
            flags.append(Flag(rule="document.truncated", severity=Severity.warn,
                              message="source ends mid-statement; trailing content may be missing",
                              path="/document/truncated"))
        return flags


RULES: list[Rule] = [
    GroundednessRule(), NumericFidelityRule(), InvariantsRule(),
    ReferentialIntegrityRule(), ReviewRule(),
]


class ValidationPipeline:
    def __init__(self, rules: list[Rule] | None = None):
        self._rules = rules if rules is not None else RULES

    def run(self, result: ExtractionResult, doc: NormalizedDocument) -> ExtractionResult:
        flags: list[Flag] = []
        for rule in self._rules:
            flags.extend(rule.check(result, doc))

        total = grounded = nulls = 0
        for _, node, _ in walk(result):
            total += 1
            ev = getattr(node, "evidence", None)
            if ev is None:
                nulls += 1
            elif doc.locate(ev.quote):
                grounded += 1
        denominator = max(total - nulls, 1)

        result.validation.flags = flags
        result.validation.metrics = ValidationMetrics(
            fields_total=total, fields_grounded=grounded, fields_null=nulls,
            groundedness_rate=round(grounded / denominator, 4))
        result.validation.passed = not any(f.severity == Severity.error for f in flags)
        result.validation.needs_review = (
            any(f.severity in (Severity.error, Severity.warn) for f in flags)
            or any(p.needs_review for p in result.packages))
        return result
