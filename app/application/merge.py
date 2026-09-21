"""Stage 4 — reconcile deterministic candidates with the LLM's reading.

Merge policy, in one sentence: numbers come from the scanners, meaning comes
from the model, and nothing enters the result without a quote that resolves
inside its own package span.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.application.preprocess import NormalizedDocument, collapse
from app.application.scanners import Candidate
from app.application.segmentation import Block
from app.domain.contracts import LlmPackageExtraction
from app.domain.models import (
    AppliesTo, BenefitMaximum, Cadence, Code, CostShare, CostShareType, Evidence, Exclusion,
    ExclusionKind, Field, FieldStatus, Limit, Package, Period, Premium, Service, ServiceCategory,
    Source, SourceSpan,
)

MONEY_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
PREVENTIVE_CATEGORIES = {ServiceCategory.preventive, ServiceCategory.diagnostic}
STOPWORDS = {"the", "and", "for", "listed", "services", "service", "dental", "your", "you", "of", "a"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "item"


def _numbers_in(quote: str) -> tuple[set[float], set[float]]:
    flat = collapse(quote)
    money = {float(m.replace(",", "")) for m in MONEY_RE.findall(flat)}
    if re.search(r"\bno copay\b", flat, re.I):
        money.add(0.0)
    return money, {float(p) for p in PCT_RE.findall(flat)}


@dataclass
class PackageAssembler:
    doc: NormalizedDocument

    # -- evidence -----------------------------------------------------------
    def evidence(self, quote: str | None, block: Block) -> Evidence | None:
        """Resolve a quote inside its own block. Returns None when the quote is
        not in the source — the caller then withholds the value."""
        if not quote:
            return None
        located = self.doc.locate(quote, within=block.span)
        if not located:
            return None
        start, end = located
        return Evidence(quote=collapse(quote), start=start, end=end, page=self.doc.page_at(start))

    def _touches_end(self, ev: Evidence | None, block: Block) -> bool:
        """Truncated means 'cut off', not merely 'last'. A complete sentence at
        a block boundary — the final exclusion before the next package heading —
        is not truncated."""
        if not ev or ev.end is None or ev.end < block.end - 1:
            return False
        return not collapse(ev.quote).rstrip().endswith((".", "!", "?", ":", ";"))

    # -- packages -----------------------------------------------------------
    def assemble(self, block: Block, candidates: list[Candidate],
                 extraction: LlmPackageExtraction | None) -> Package:
        by_kind: dict[str, list[Candidate]] = {}
        for c in candidates:
            by_kind.setdefault(c.kind, []).append(c)

        pkg = Package(
            package_id=block.block_id,
            ordinal=block.ordinal,
            name=self._name(block),
            premium=self._premium(by_kind.get("premium", []), block),
            benefit_maximum=self._benefit_max(by_kind.get("benefit_maximum", []), block),
            network_restriction=self._network(by_kind.get("network_restriction", []), extraction, block),
            codes=self._codes(by_kind.get("code", []), block),
            source_span=SourceSpan(start=block.start, end=block.end, pages=list(block.pages)),
            degraded=extraction is None,
        )
        pkg.benefit_domains = self._domains(extraction, block)
        pkg.services = self._services(extraction, by_kind.get("limit", []), pkg, block)
        pkg.cost_share = self._cost_share(extraction, by_kind.get("cost_share", []), pkg, block)
        pkg.exclusions = self._exclusions(extraction, block)
        return pkg

    def _name(self, block: Block) -> Field[str]:
        ev = self.evidence(block.heading, block)
        if not ev:
            return Field[str].missing("package heading could not be anchored")
        return Field[str](value=collapse(block.heading or ""), status=FieldStatus.found,
                          confidence=0.99, source=Source.deterministic, evidence=ev)

    def _premium(self, cands: list[Candidate], block: Block) -> Field[Premium]:
        if not cands:
            return Field[Premium].missing()
        if len({c.value["amount_usd"] for c in cands}) > 1:
            ev = self.evidence(cands[0].quote, block)
            return Field[Premium](value=None, status=FieldStatus.ambiguous, evidence=ev,
                                  source=Source.deterministic,
                                  notes=f"{len(cands)} different premiums inside one package span")
        c = cands[0]
        return Field[Premium](
            value=Premium(amount_usd=c.value["amount_usd"], cadence=c.value["cadence"] or Cadence.monthly),
            status=FieldStatus.found, confidence=c.confidence, source=Source.deterministic,
            evidence=self.evidence(c.quote, block))

    def _benefit_max(self, cands: list[Candidate], block: Block) -> Field[BenefitMaximum]:
        if not cands:
            return Field[BenefitMaximum].missing()
        if len({c.value["amount_usd"] for c in cands}) > 1:
            return Field[BenefitMaximum](value=None, status=FieldStatus.ambiguous,
                                         source=Source.deterministic,
                                         evidence=self.evidence(cands[0].quote, block),
                                         notes="conflicting benefit maximums in one package")
        c = cands[0]
        return Field[BenefitMaximum](
            value=BenefitMaximum(amount_usd=c.value["amount_usd"],
                                 period=c.value["period"] or Period.per_year,
                                 applies_to=c.value["applies_to"]),
            status=FieldStatus.found, confidence=c.confidence, source=Source.deterministic,
            evidence=self.evidence(c.quote, block))

    def _network(self, cands: list[Candidate], extraction: LlmPackageExtraction | None,
                 block: Block) -> Field[str]:
        llm_value = extraction.network_restriction if extraction else None
        if cands:
            c = cands[0]
            agreed = bool(llm_value and collapse(llm_value).lower() in collapse(c.quote).lower())
            return Field[str](value=c.value, status=FieldStatus.found,
                              confidence=min(0.99, c.confidence + (0.02 if agreed else 0)),
                              source=Source.merged if agreed else Source.deterministic,
                              evidence=self.evidence(c.quote, block))
        if llm_value and (ev := self.evidence(extraction.network_restriction_evidence or llm_value, block)):
            return Field[str](value=collapse(llm_value), status=FieldStatus.found, confidence=0.8,
                              source=Source.llm, evidence=ev)
        return Field[str].missing()

    def _codes(self, cands: list[Candidate], block: Block) -> list[Code]:
        seen: dict[str, Code] = {}
        for c in cands:
            code = c.value["code"]
            if code in seen:
                continue
            seen[code] = Code(code=code, description=c.value["description"] or None,
                              evidence=self.evidence(c.quote, block))
        return list(seen.values())

    def _domains(self, extraction: LlmPackageExtraction | None, block: Block) -> list[str]:
        if extraction and extraction.benefit_domains:
            return sorted({d.lower() for d in extraction.benefit_domains})
        heading = (block.heading or "").lower()
        return sorted({d for d in ("dental", "vision", "hearing") if d in heading})

    # -- services -----------------------------------------------------------
    def _services(self, extraction: LlmPackageExtraction | None, limit_cands: list[Candidate],
                  pkg: Package, block: Block) -> list[Service]:
        if not extraction:
            return []
        declared = {c.code for c in pkg.codes}
        out: list[Service] = []
        for svc in extraction.services:
            ev = self.evidence(svc.evidence_quote, block)
            if not ev:                                    # ungrounded -> not a fact
                out.append(Service(
                    service_id=f"{pkg.package_id}.svc_{_slug(svc.name)}", name=svc.name,
                    status=FieldStatus.unverified, source=Source.llm, confidence=0.0,
                    notes="evidence quote not found in the source; service withheld"))
                continue
            limit, note = self._limit_for(svc, ev, limit_cands)
            out.append(Service(
                service_id=f"{pkg.package_id}.svc_{_slug(svc.name)}",
                name=svc.name,
                category=self._category(svc.category),
                limit=limit,
                codes=[c for c in svc.codes if c in declared],
                status=FieldStatus.truncated if self._touches_end(ev, block) and extraction.truncated
                else FieldStatus.found,
                confidence=0.9 if note is None else 0.8,
                source=Source.merged if note is None else Source.llm,
                evidence=ev,
                notes=self._notes(note, self._dropped_codes_note(svc.codes, declared)),
            ))
        return out

    @staticmethod
    def _notes(*parts: str | None) -> str | None:
        return "; ".join(p for p in parts if p) or None

    @staticmethod
    def _dropped_codes_note(codes: list[str], declared: set[str]) -> str | None:
        dropped = [c for c in codes if c not in declared]
        return f"codes not present in this package were dropped: {', '.join(dropped)}" if dropped else None

    def _limit_for(self, svc, ev: Evidence, limit_cands: list[Candidate]) -> tuple[Limit | None, str | None]:
        """Deterministic count wins over the model's count for the same phrase."""
        if ev.start is None:
            return (None, None)
        ev_end = ev.end if ev.end is not None else ev.start
        match = next((c for c in limit_cands if c.start < ev_end and ev.start < c.end), None)
        if match:
            det = match.value
            note = None
            if svc.limit_count is not None and det["count"] is not None and svc.limit_count != det["count"]:
                note = (f"model said count={svc.limit_count}; scanner read {det['count']} "
                        "from the same phrase - scanner wins")
            return (Limit(count=det["count"], unit=det["unit"], period=det["period"],
                          raw=collapse(match.quote)), note)
        # No scanner match: keep the model's limit only if its number is in the quote.
        if svc.limit_count is not None and svc.limit_count not in _numbers_in(ev.quote)[0] \
                and str(int(svc.limit_count)) not in collapse(ev.quote):
            return (Limit(count=None, unit=svc.limit_unit, period=self._period(svc.limit_period),
                          raw=svc.limit_raw), "model's limit count was not in its evidence; dropped")
        return (Limit(count=svc.limit_count, unit=svc.limit_unit,
                      period=self._period(svc.limit_period), raw=svc.limit_raw), None)

    @staticmethod
    def _category(value: str | None) -> ServiceCategory | None:
        try:
            return ServiceCategory(value) if value else None
        except ValueError:
            return ServiceCategory.other

    @staticmethod
    def _period(value: str | None) -> Period | None:
        try:
            return Period(value) if value else None
        except ValueError:
            return None

    # -- cost share ---------------------------------------------------------
    def _cost_share(self, extraction: LlmPackageExtraction | None, cands: list[Candidate],
                    pkg: Package, block: Block) -> list[CostShare]:
        llm_items = []
        if extraction:
            for item in extraction.cost_share:
                ev = self.evidence(item.evidence_quote, block)
                if ev:
                    llm_items.append((item, ev))

        out: list[CostShare] = []
        used: set[int] = set()
        for i, c in enumerate(cands):                      # scanner-owned numbers
            ev = self.evidence(c.quote, block)
            pair = next(((j, item, iev) for j, (item, iev) in enumerate(llm_items)
                         if j not in used and iev.start is not None and ev is not None
                         and abs((iev.start or 0) - (ev.start or 0)) < 40), None)
            description = c.value["applies_to"]
            note = None
            if pair:
                j, item, _ = pair
                used.add(j)
                description = item.applies_to or description
                note = self._contradiction_note(item, c.value)
            out.append(CostShare(
                cost_share_id=f"{pkg.package_id}.cs_{i + 1}",
                type=CostShareType(c.value["type"]),
                amount_usd=c.value["amount_usd"], percent=c.value["percent"],
                applies_to=AppliesTo(description=description,
                                     service_ids=self._match_services(description, pkg.services)),
                status=FieldStatus.truncated if self._touches_end(ev, block) else FieldStatus.found,
                confidence=c.confidence if note is None else min(c.confidence, 0.75),
                source=Source.merged if pair else Source.deterministic,
                evidence=ev, notes=note))

        for j, (item, ev) in enumerate(llm_items):         # model-only cost shares
            if j in used:
                continue
            money, pcts = _numbers_in(ev.quote)
            claimed_ok = ((item.amount_usd is None or item.amount_usd in money)
                          and (item.percent is None or item.percent in pcts))
            out.append(CostShare(
                cost_share_id=f"{pkg.package_id}.cs_{len(out) + 1}",
                type=CostShareType(item.type) if item.type in CostShareType.__members__
                else CostShareType.unknown,
                amount_usd=item.amount_usd if claimed_ok else None,
                percent=item.percent if claimed_ok else None,
                applies_to=AppliesTo(description=item.applies_to,
                                     service_ids=self._match_services(item.applies_to, pkg.services)),
                status=FieldStatus.found if claimed_ok else FieldStatus.unverified,
                confidence=0.85 if claimed_ok else 0.0, source=Source.llm, evidence=ev,
                notes=None if claimed_ok else "amount/percent not present in the evidence quote; withheld"))
        return out

    @staticmethod
    def _contradiction_note(item, scanned: dict) -> str | None:
        """The model and the scanner read the same sentence differently. The
        scanner's digits win, but the disagreement is recorded rather than
        silently dropped — it is the signal that the prompt or the model drifted."""
        claims = []
        if item.percent is not None and item.percent != scanned["percent"]:
            claims.append(f"percent={item.percent}")
        if item.amount_usd is not None and item.amount_usd != scanned["amount_usd"]:
            claims.append(f"amount_usd={item.amount_usd}")
        if not claims:
            return None
        return (f"model claimed {', '.join(claims)}; scanner read "
                f"percent={scanned['percent']}, amount_usd={scanned['amount_usd']} "
                "from the same sentence - scanner wins")

    @staticmethod
    def _match_services(description: str | None, services: list[Service]) -> list[str]:
        """Attach a cost share to services by name overlap, plus the one domain
        rule that matters here: 'preventive' covers preventive + diagnostic."""
        if not description:
            return []
        low = collapse(description).lower()
        matched = []
        for svc in services:
            if svc.status != FieldStatus.found:
                continue
            tokens = {t for t in re.split(r"\W+", svc.name.lower()) if t and t not in STOPWORDS}
            if (svc.name.lower() in low) or (tokens and all(t.rstrip("s") in low for t in tokens)):
                matched.append(svc.service_id)
            elif "preventive" in low and svc.category in PREVENTIVE_CATEGORIES:
                matched.append(svc.service_id)
        return sorted(set(matched))

    # -- exclusions ---------------------------------------------------------
    def _exclusions(self, extraction: LlmPackageExtraction | None, block: Block) -> list[Exclusion]:
        if not extraction:
            return []
        out: list[Exclusion] = []
        for i, item in enumerate(extraction.exclusions, start=1):
            ev = self.evidence(item.evidence_quote, block)
            if not ev:
                continue                                   # ungrounded exclusions are dropped
            try:
                kind = ExclusionKind(item.kind) if item.kind else None
            except ValueError:
                kind = ExclusionKind.other
            out.append(Exclusion(
                exclusion_id=f"{block.block_id}.ex_{i}", text=collapse(item.text), kind=kind,
                status=FieldStatus.truncated if self._touches_end(ev, block) else FieldStatus.found,
                confidence=0.9, source=Source.llm, evidence=ev))
        return out
