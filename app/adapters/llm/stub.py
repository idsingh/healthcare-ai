"""Offline stand-in for the model.

It implements the same port and honours the same contract (verbatim quotes,
nulls instead of guesses), so the whole service — API, pipeline, validation —
runs end to end with no API key, in tests and in local development.

It is a stand-in, not a model: it applies obvious surface heuristics to the
block it is given. Anything genuinely semantic will look thin here, which is
the honest signal that the real model is doing that part.
"""
from __future__ import annotations

import re

from app.application.preprocess import collapse

DOC_RE = re.compile(r"<document[^>]*>(.*?)</document>", re.S)
PACKAGE_RE = re.compile(r"(?:Optional\s+)?supplemental\s+package\s+\d+\s*[-:]\s*[^\n.]{3,80}", re.I)
NETWORK_RE = re.compile(r"Coverage is available from\s+(.{3,60}?providers only)", re.I)
SERVICE_RE = re.compile(
    r"\b(?:One|Two|Three|Four|Five|Six|Seven|\d{1,2})\s+[A-Za-z][A-Za-z \-]{2,40}?\s+"
    r"(?:each year|per year|per calendar year|each month|per month)\b", re.I)
XRAY_RE = re.compile(r"Dental X-rays include[^.]*\.", re.I)
ZERO_COPAY_RE = re.compile(r"You pay no copay for\s+([^.]{3,120})\.", re.I)
COINS_RE = re.compile(r"You pay\s+(\d{1,3})\s?%[^.]*?for\s+([^.]{3,160})(?:\.|$)", re.I)
CODE_RE = re.compile(r"\bD\d{4}\b")
COUNT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
DOMAIN_WORDS = {"dental": "dental", "vision": "vision", "hearing": "hearing", "over-the-counter": "otc"}
CATEGORIES = [
    ("exam", "diagnostic"), ("x-ray", "diagnostic"), ("cleaning", "preventive"),
    ("prophylaxis", "preventive"), ("fluoride", "preventive"), ("filling", "restorative"),
    ("restorative", "restorative"), ("endodontic", "endodontic"), ("periodontic", "periodontic"),
    ("oral surgery", "oral_surgery"), ("vision", "vision"), ("eyewear", "vision"),
]


class StubLLMClient:
    model_id = "stub/deterministic-offline"

    async def complete_json(self, *, system: str, user: str, json_schema: dict,
                            schema_name: str, seed: int | None = None,
                            temperature: float = 0.0) -> dict:
        m = DOC_RE.search(user)
        flat = collapse(m.group(1) if m else user)
        return {
            "package_name": self._first(PACKAGE_RE, flat),
            "package_name_evidence": self._first(PACKAGE_RE, flat),
            "benefit_domains": sorted({v for k, v in DOMAIN_WORDS.items() if k in flat.lower()}),
            "network_restriction": (net := self._network(flat))[0],
            "network_restriction_evidence": net[1],
            "services": self._services(flat),
            "cost_share": self._cost_share(flat),
            "exclusions": self._exclusions(flat),
            "truncated": not flat.rstrip().endswith((".", ":", ";")),
        }

    @staticmethod
    def _first(pattern: re.Pattern[str], text: str) -> str | None:
        m = pattern.search(text)
        return m.group(0).strip() if m else None

    @staticmethod
    def _network(text: str) -> tuple[str | None, str | None]:
        m = NETWORK_RE.search(text)
        return (m.group(1).strip(), m.group(0).strip()) if m else (None, None)

    def _services(self, flat: str) -> list[dict]:
        xrays = list(XRAY_RE.finditer(flat))
        spans = [(m.start(), m.end()) for m in xrays]
        matches = [m for m in SERVICE_RE.finditer(flat)
                   if not any(s <= m.start() and m.end() <= e for s, e in spans)] + xrays
        matches.sort(key=lambda m: m.start())
        out: list[dict] = []
        for i, m in enumerate(matches):
            quote = m.group(0).strip()
            if re.match(r"^\$|\d[\d,]*\s+for\b", quote):
                continue
            window = flat[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(flat)]
            name, count, unit, period = self._parse_service(quote)
            out.append({
                "name": name,
                "category": next((c for k, c in CATEGORIES if k in name.lower()), "other"),
                "limit_count": count,
                "limit_unit": unit,
                "limit_period": period,
                "limit_raw": quote,
                "codes": CODE_RE.findall(window),
                "evidence_quote": quote,
            })
        return out

    @staticmethod
    def _parse_service(quote: str) -> tuple[str, float | None, str | None, str | None]:
        m = re.match(r"^(?P<count>\w+)\s+(?P<unit>.+?)\s+(?P<period>each year|per year|per calendar year"
                     r"|each month|per month)$", quote, re.I)
        if not m:
            return ("Dental X-rays", None, "study", "per_year")
        raw = m["count"].lower()
        count = float(COUNT_WORDS.get(raw, raw)) if raw.isdigit() or raw in COUNT_WORDS else None
        period = "per_calendar_year" if "calendar" in m["period"].lower() else (
            "per_month" if "month" in m["period"].lower() else "per_year")
        unit = m["unit"].strip()
        return (unit[:1].upper() + unit[1:], count, unit.lower(), period)

    @staticmethod
    def _cost_share(flat: str) -> list[dict]:
        out: list[dict] = []
        for m in ZERO_COPAY_RE.finditer(flat):
            out.append({"type": "copay", "amount_usd": 0.0, "percent": None,
                        "applies_to": m.group(1).strip(), "evidence_quote": m.group(0).strip()})
        for m in COINS_RE.finditer(flat):
            out.append({"type": "coinsurance", "amount_usd": None, "percent": float(m.group(1)),
                        "applies_to": m.group(2).strip(), "evidence_quote": m.group(0).strip()})
        return out

    @staticmethod
    def _exclusions(flat: str) -> list[dict]:
        out: list[dict] = []
        for chunk in flat.split("•")[1:]:
            text = chunk.strip().split("Optional supplemental package")[0].strip()
            text = re.split(r"(?<=\.)\s+(?=[A-Z])", text)[0].strip()
            if not text or CODE_RE.match(text) or len(text) < 25:
                continue
            low = text.lower()
            kind = ("network_restriction" if "contracted provider" in low and "rendered" in low else
                    "claims_process" if "claims" in low else
                    "accumulator" if "out-of-pocket" in low else
                    "member_liability" if low.startswith("you must pay") else
                    "service_excluded" if "excluded" in low else "other")
            out.append({"text": text, "kind": kind, "evidence_quote": text})
        return out
