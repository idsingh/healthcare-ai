"""Stage 3a — deterministic scanners.

Rule: regex owns the digits, the LLM owns the meaning. Every number that ends
up in the output is read by one of these, never retyped by a model.

Each scanner is small and single-purpose (SRP) and they are registered in a
list (OCP): adding a benefit domain means appending scanners, not editing the
pipeline. They are deliberately not merged into one clever mega-regex.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from app.application.preprocess import collapse
from app.application.segmentation import Block, logical_lines
from app.domain.models import Cadence, Period

MONEY = r"\$\s?(?P<amt>\d[\d,]*(?:\.\d{1,2})?)"
COUNT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
               "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


@dataclass(frozen=True)
class Candidate:
    kind: str
    value: Any
    quote: str
    start: int
    end: int
    confidence: float = 0.97
    source: str = "deterministic"


def _to_float(text: str) -> float:
    return float(text.replace(",", "").replace("$", "").strip())


def _cadence(word: str | None) -> Cadence | None:
    if not word:
        return None
    w = word.lower()
    if "month" in w:
        return Cadence.monthly
    if "annual" in w or "year" in w:
        return Cadence.annual
    return None


def _period(word: str | None) -> Period | None:
    if not word:
        return None
    w = word.lower()
    if "calendar year" in w:
        return Period.per_calendar_year
    if "year" in w:
        return Period.per_year
    if "month" in w:
        return Period.per_month
    if "lifetime" in w:
        return Period.per_lifetime
    return None


class Scanner(Protocol):
    kind: str

    def scan(self, block: Block) -> Iterable[Candidate]: ...


class _RegexScanner:
    """Base for the common shape: one pattern, one candidate per match."""
    kind = "generic"
    pattern: re.Pattern[str]
    confidence = 0.97

    def scan(self, block: Block) -> list[Candidate]:
        return [
            Candidate(self.kind, self.build(m), m.group(0),
                      block.start + m.start(), block.start + m.end(), self.confidence)
            for m in self.pattern.finditer(block.flat)
        ]

    def build(self, m: re.Match[str]) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError


class PremiumScanner(_RegexScanner):
    kind = "premium"
    pattern = re.compile(r"Premium\s+" + MONEY + r"\s*(?P<cad>monthly|annual|per month)?\s*premium?", re.I)
    confidence = 0.99

    def build(self, m):
        return {"amount_usd": _to_float(m["amt"]), "cadence": _cadence(m["cad"])}


class BenefitMaximumScanner(_RegexScanner):
    kind = "benefit_maximum"
    pattern = re.compile(
        r"plan will pay up to\s+" + MONEY + r"\s+for\s+(?P<scope>.{3,80}?)\s+"
        r"(?P<period>each year|per year|per calendar year)", re.I)
    confidence = 0.97

    def build(self, m):
        return {"amount_usd": _to_float(m["amt"]), "period": _period(m["period"]),
                "applies_to": m["scope"].strip()}


class NetworkRestrictionScanner(_RegexScanner):
    kind = "network_restriction"
    pattern = re.compile(r"Coverage is available from\s+(?P<net>.{3,60}?providers only)", re.I)
    confidence = 0.95

    def build(self, m):
        return m["net"].strip()


class CdtCodeScanner:
    """Codes and their descriptions are pure string work; the LLM is only asked
    to repair bullets this cannot split."""
    kind = "code"
    pattern = re.compile(r"\b(?P<code>D\d{4})\b\s*[-–]\s*(?P<desc>.+?)(?=\s*(?:•|\bD\d{4}\b)|$)")

    def scan(self, block: Block) -> list[Candidate]:
        """Scanned per logical line, not over the whole flattened block: a
        description must not run on into whatever text follows the bullet."""
        out: list[Candidate] = []
        cursor = 0
        for line in logical_lines(block.text):
            for m in self.pattern.finditer(collapse(line)):
                quote = m.group(0).strip()
                idx = block.flat.find(quote, cursor)
                if idx == -1:
                    idx = block.flat.find(quote)
                if idx == -1:
                    continue
                cursor = idx + len(quote)
                desc = re.sub(r"\s*[•·]\s*$", "", m["desc"]).strip(" .;")
                out.append(Candidate(self.kind, {"code": m["code"], "description": desc},
                                     quote, block.start + idx, block.start + idx + len(quote), 0.97))
        return out


class ZeroCopayScanner(_RegexScanner):
    kind = "cost_share"
    pattern = re.compile(r"You pay no copay for\s+(?P<scope>[^.]{3,120})\.", re.I)
    confidence = 0.94

    def build(self, m):
        return {"type": "copay", "amount_usd": 0.0, "percent": None, "applies_to": m["scope"].strip()}


class CoinsuranceScanner(_RegexScanner):
    kind = "cost_share"
    pattern = re.compile(
        r"You pay\s+(?P<pct>\d{1,3})\s?%\s+(?:as your portion of the covered charges\s+)?"
        r"for\s+(?P<scope>[^.]{3,160})(?:\.|$)", re.I)
    confidence = 0.94

    def build(self, m):
        return {"type": "coinsurance", "amount_usd": None, "percent": float(m["pct"]),
                "applies_to": m["scope"].strip()}


class CopayAmountScanner(_RegexScanner):
    kind = "cost_share"
    pattern = re.compile(r"You pay\s+" + MONEY + r"\s+(?:copay\s+)?for\s+(?P<scope>[^.]{3,160})(?:\.|$)", re.I)
    confidence = 0.94

    def build(self, m):
        return {"type": "copay", "amount_usd": _to_float(m["amt"]), "percent": None,
                "applies_to": m["scope"].strip()}


class FrequencyLimitScanner(_RegexScanner):
    """'Two cleanings per year' -> count=2, unit='cleanings', period=per_year."""
    kind = "limit"
    pattern = re.compile(
        r"(?<![\d.,$])\b(?P<count>one|two|three|four|five|six|seven|eight|nine|ten|\d{1,3})\s+"
        r"(?P<unit>[A-Za-z][A-Za-z \-]{2,40}?)\s+"
        r"(?P<period>each year|per year|per calendar year|each month|per month)\b", re.I)
    confidence = 0.9

    def scan(self, block: Block) -> list[Candidate]:
        out: list[Candidate] = []
        for m in self.pattern.finditer(block.flat):
            unit = m["unit"].strip().lower()
            if "premium" in unit:          # '...monthly premium Two cleanings per year'
                continue
            if unit.startswith(("for ", "of ", "and ")):
                continue
            out.append(Candidate(self.kind, self.build(m), m.group(0),
                                 block.start + m.start(), block.start + m.end(), self.confidence))
        return out

    def build(self, m):
        raw = m["count"].lower()
        return {"count": float(COUNT_WORDS.get(raw, raw if raw.isdigit() else 0)) or None,
                "unit": m["unit"].strip().lower(), "period": _period(m["period"])}


class PlanHeaderScanner:
    """Document-level facts, scanned only on the header block."""
    kind = "document"
    year = re.compile(r"\b(?P<year>20\d{2})\s+Evidence of Coverage for\s+(?P<plan>[^\n]{3,80})")
    form = re.compile(r"(?P<form>[A-Z]{2,}-?[A-Z]*\s+\d{5,}[A-Z0-9_]*)\s+Revised", re.I)

    def scan(self, block: Block) -> list[Candidate]:
        out: list[Candidate] = []
        if m := self.year.search(block.flat):
            plan = m["plan"].split(" Chapter")[0].strip(" .")
            out.append(Candidate("plan_name", plan, m.group(0),
                                 block.start + m.start(), block.start + m.end(), 0.95))
            out.append(Candidate("plan_year", int(m["year"]), m.group(0),
                                 block.start + m.start(), block.start + m.end(), 0.99))
            out.append(Candidate("document_type", "evidence_of_coverage", m.group(0),
                                 block.start + m.start(), block.start + m.end(), 0.97))
        if m := self.form.search(block.flat):
            out.append(Candidate("form_id", m["form"].strip(), m.group(0),
                                 block.start + m.start(), block.start + m.end(), 0.9))
        return out


PACKAGE_SCANNERS: list[Scanner] = [
    PremiumScanner(), BenefitMaximumScanner(), NetworkRestrictionScanner(), CdtCodeScanner(),
    ZeroCopayScanner(), CoinsuranceScanner(), CopayAmountScanner(), FrequencyLimitScanner(),
]
HEADER_SCANNERS: list[Scanner] = [PlanHeaderScanner()]


def scan_block(block: Block) -> list[Candidate]:
    scanners = HEADER_SCANNERS if block.kind == "header" else PACKAGE_SCANNERS
    return [c for s in scanners for c in s.scan(block)]


def render_prescan(candidates: list[Candidate]) -> str:
    """Literals handed to the model so it never has to retype a number."""
    lines = []
    for c in candidates:
        if c.kind in {"code", "limit"}:
            continue
        lines.append(f"- {c.kind}: {c.value!r} (from: {c.quote.strip()!r})")
    codes = [c.value["code"] for c in candidates if c.kind == "code"]
    if codes:
        lines.append(f"- codes present in this block: {', '.join(codes)}")
    return "\n".join(lines) or "- (no literals found)"
