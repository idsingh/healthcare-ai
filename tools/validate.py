#!/usr/bin/env python3
"""Runnable slice of the validation pipeline (DESIGN.md section 7).

    python3 tools/validate.py                 # validate sample_output.json
    python3 tools/validate.py --fix-offsets   # resolve evidence spans, then validate

Same normalizer as ingestion (DRY): "grounded" means one thing everywhere.
Rules are independent objects (OCP) collected by a composite pipeline; adding a
rule is adding a function to RULES, never editing the runner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "extracted_text.txt"
OUTPUT = ROOT / "sample_output.json"

DASHES = {"–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-"}
MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
CODE_RE = re.compile(r"^D\d{4}$")


# ---------------------------------------------------------------- normalizer
def normalize(text: str) -> str:
    """NFKC -> dash fold -> soft-hyphen removal -> whitespace collapse.

    Idempotent and cheap; the ingestion path uses the same function and also
    keeps an offset map back to the raw file (omitted here for brevity).
    """
    text = unicodedata.normalize("NFKC", text)
    text = "".join(DASHES.get(ch, ch) for ch in text)
    text = text.replace("­", "")
    text = re.sub(r"-\n(?=[a-z])", "", text)      # de-hyphenate wrapped words
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------------- results
@dataclass
class Flag:
    rule: str
    severity: str  # error | warn | info
    message: str
    path: str | None = None


@dataclass
class Context:
    source: str
    doc: dict[str, Any]
    flags: list[Flag] = dc_field(default_factory=list)
    counts: dict[str, int] = dc_field(default_factory=lambda: {"total": 0, "grounded": 0, "null": 0})

    def flag(self, rule: str, severity: str, message: str, path: str | None = None) -> None:
        self.flags.append(Flag(rule, severity, message, path))


def iter_fields(node: Any, path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield every node carrying an `evidence` key: Field<T>, code, service,
    cost_share and exclusion all share that contract (LSP)."""
    if isinstance(node, dict):
        if "evidence" in node:
            yield path, node
        for key, value in node.items():
            yield from iter_fields(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from iter_fields(value, f"{path}/{i}")


def package_spans(ctx: Context) -> dict[str, tuple[int, int]]:
    """Char span of each package, used to scope evidence resolution so a phrase
    repeated in two packages ('LIBERTY Dental providers only') cannot be
    anchored to the wrong one."""
    starts: list[tuple[str, int]] = []
    for pkg in ctx.doc["packages"]:
        quote = normalize(pkg["name"]["evidence"]["quote"])
        idx = ctx.source.find(quote)
        starts.append((pkg["package_id"], idx))
    spans: dict[str, tuple[int, int]] = {}
    for i, (pid, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) and starts[i + 1][1] > start else len(ctx.source)
        spans[pid] = (start, end)
    return spans


def owning_package(path: str, doc: dict[str, Any]) -> str | None:
    m = re.match(r"^/packages/(\d+)", path)
    return doc["packages"][int(m.group(1))]["package_id"] if m else None


# --------------------------------------------------------------------- rules
def rule_groundedness(ctx: Context) -> None:
    """1. Every evidence quote must be a verbatim substring of the normalized
    source, and its offsets must resolve to that quote."""
    spans = package_spans(ctx)
    for path, node in iter_fields(ctx.doc):
        ev = node.get("evidence")
        ctx.counts["total"] += 1
        if ev is None:
            ctx.counts["null"] += 1
            if node.get("status") not in {"not_stated", "truncated", None}:
                ctx.flag("groundedness.missing_evidence", "error",
                         f"status={node.get('status')} but no evidence", path)
            continue
        quote = normalize(ev["quote"])
        pid = owning_package(path, ctx.doc)
        window = spans.get(pid) if pid else (0, len(ctx.source))
        found = ctx.source.find(quote, *window) if window else -1
        if found == -1:
            found = ctx.source.find(quote)           # fall back to whole doc
            if found != -1 and pid:
                ctx.flag("groundedness.out_of_package_span", "warn",
                         f"quote resolves outside {pid}'s span", path)
        if found == -1:
            ctx.flag("groundedness.not_in_source", "error",
                     f"evidence not found in source: {ev['quote'][:60]!r}", path)
            node["status"] = "unverified"
            if "value" in node:
                node["value"] = None
            continue
        ctx.counts["grounded"] += 1
        if ev.get("start") is not None and ctx.source[ev["start"]:ev["end"]] != quote:
            ctx.flag("groundedness.offset_mismatch", "error",
                     "start/end do not resolve to the quote", path)


def rule_numeric_fidelity(ctx: Context) -> None:
    """2. Every amount/percent must appear literally in its own evidence."""
    for path, node in iter_fields(ctx.doc):
        ev = node.get("evidence")
        if not ev:
            continue
        quote = normalize(ev["quote"])
        amounts = {float(m.replace(",", "")) for m in MONEY_RE.findall(quote)}
        percents = {float(p) for p in PCT_RE.findall(quote)}
        if "no copay" in quote.lower() or "$0" in quote:
            amounts.add(0.0)
        for num in numeric_claims(node):
            kind, val = num
            pool = amounts if kind == "money" else percents
            if val == 0 and kind == "money" and "no copay" in quote.lower():
                continue
            if val not in pool:
                ctx.flag("numeric.not_in_evidence", "error",
                         f"{kind} {val} is not present in its evidence quote", path)


def numeric_claims(node: dict[str, Any]) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    val = node.get("value")
    targets = [val] if isinstance(val, dict) else []
    targets.append(node)
    for t in targets:
        if not isinstance(t, dict):
            continue
        if isinstance(t.get("amount_usd"), (int, float)):
            out.append(("money", float(t["amount_usd"])))
        if isinstance(t.get("percent"), (int, float)):
            out.append(("percent", float(t["percent"])))
    return out


def rule_invariants(ctx: Context) -> None:
    """3. Completeness and contradiction checks per package."""
    for i, pkg in enumerate(ctx.doc["packages"]):
        base = f"/packages/{i}"
        premium = pkg["premium"]
        if premium.get("status") == "not_stated":
            ctx.flag("package.premium_missing", "error",
                     f"{pkg['package_id']} is an optional supplemental package with no premium stated", f"{base}/premium")
        for key in ("premium", "benefit_maximum"):
            v = pkg[key].get("value")
            if isinstance(v, dict) and v.get("amount_usd") is not None and v["amount_usd"] <= 0:
                ctx.flag(f"package.{key}_non_positive", "error", f"{key} must be > 0", f"{base}/{key}")
        for j, cs in enumerate(pkg.get("cost_share", [])):
            if cs.get("percent") is not None and not 0 <= cs["percent"] <= 100:
                ctx.flag("cost_share.percent_range", "error", "coinsurance outside 0-100", f"{base}/cost_share/{j}")
            if cs["type"] == "coinsurance" and cs.get("percent") is None:
                ctx.flag("cost_share.coinsurance_without_percent", "error", "coinsurance with no percent", f"{base}/cost_share/{j}")
            if cs["type"] == "copay" and cs.get("amount_usd") is None:
                ctx.flag("cost_share.copay_without_amount", "error", "copay with no amount", f"{base}/cost_share/{j}")
        # contradiction: same service set carrying both a copay and a coinsurance
        seen: dict[str, set[str]] = {}
        for cs in pkg.get("cost_share", []):
            for sid in cs["applies_to"].get("service_ids", []):
                seen.setdefault(sid, set()).add(cs["type"])
        for sid, types in seen.items():
            if {"copay", "coinsurance"} <= types:
                ctx.flag("cost_share.conflicting_types", "error",
                         f"{sid} carries both a copay and a coinsurance", base)


def rule_referential_integrity(ctx: Context) -> None:
    """4. Code shape, and every service code must exist in the package's code list."""
    for i, pkg in enumerate(ctx.doc["packages"]):
        declared = {c["code"] for c in pkg.get("codes", [])}
        for c in pkg.get("codes", []):
            if not CODE_RE.match(c["code"]):
                ctx.flag("code.bad_shape", "error", f"{c['code']} is not a CDT code", f"/packages/{i}/codes")
            if not c.get("description"):
                ctx.flag("code.no_description", "warn", f"{c['code']} has no description", f"/packages/{i}/codes")
        for j, svc in enumerate(pkg.get("services", [])):
            for code in svc.get("codes", []):
                if code not in declared:
                    ctx.flag("service.unknown_code", "error",
                             f"{code} referenced by {svc['service_id']} is not in the package code list",
                             f"/packages/{i}/services/{j}")
        for j, cs in enumerate(pkg.get("cost_share", [])):
            known = {s["service_id"] for s in pkg.get("services", [])}
            for sid in cs["applies_to"].get("service_ids", []):
                if sid not in known:
                    ctx.flag("cost_share.unknown_service", "error", f"{sid} does not exist", f"/packages/{i}/cost_share/{j}")


def rule_exclusions_observed(ctx: Context) -> None:
    """An empty exclusions list means 'not observed in this text', never 'none apply'."""
    for i, pkg in enumerate(ctx.doc["packages"]):
        if not pkg.get("exclusions"):
            ctx.flag("package.exclusions_not_stated", "warn",
                     f"no exclusions section observed for {pkg['package_id']}; "
                     "empty list is not an assertion that none apply", f"/packages/{i}/exclusions")


def rule_ambiguity(ctx: Context) -> None:
    """5. Anything not cleanly grounded routes to human review."""
    review_states = {"ambiguous", "unverified", "truncated"}
    for i, pkg in enumerate(ctx.doc["packages"]):
        needs = False
        for path, node in iter_fields(pkg, f"/packages/{i}"):
            conf = node.get("confidence")
            if node.get("status") in review_states or (isinstance(conf, (int, float)) and conf < 0.7):
                needs = True
                severity = "warn" if node.get("status") in review_states else "info"
                ctx.flag("review.low_certainty", severity,
                         f"status={node.get('status')} confidence={conf}", path)
        if needs and not pkg.get("needs_review"):
            ctx.flag("review.flag_missing", "error",
                     f"{pkg['package_id']} has low-certainty fields but needs_review is false", f"/packages/{i}")
        pkg["needs_review"] = pkg.get("needs_review", False) or needs
    if ctx.doc["document"].get("truncated"):
        ctx.flag("document.truncated", "warn", "source ends mid-sentence", "/document/truncated")


RULES: list[Callable[[Context], None]] = [
    rule_groundedness,
    rule_exclusions_observed,
    rule_numeric_fidelity,
    rule_invariants,
    rule_referential_integrity,
    rule_ambiguity,
]


# ----------------------------------------------------------------- offset fix
def fix_offsets(ctx: Context) -> None:
    spans = package_spans(ctx)
    for path, node in iter_fields(ctx.doc):
        ev = node.get("evidence")
        if not ev:
            continue
        quote = normalize(ev["quote"])
        pid = owning_package(path, ctx.doc)
        start = ctx.source.find(quote, *spans[pid]) if pid in spans else -1
        if start == -1:
            start = ctx.source.find(quote)
        if start != -1:
            ev["start"], ev["end"] = start, start + len(quote)
    for i, pkg in enumerate(ctx.doc["packages"]):
        s, e = spans[pkg["package_id"]]
        pkg["source_span"]["start"], pkg["source_span"]["end"] = s, e
    raw = SOURCE.read_bytes()
    ctx.doc["run"]["content_sha256"] = hashlib.sha256(raw).hexdigest()
    ctx.doc["run"]["idempotency_key"] = hashlib.sha256("|".join([
        ctx.doc["run"]["content_sha256"], ctx.doc["schema_version"],
        ctx.doc["run"]["prompt_version"] or "", ctx.doc["run"]["model_id"] or "",
        json.dumps(ctx.doc["run"]["model_params"], sort_keys=True),
    ]).encode()).hexdigest()
    ctx.doc["document"]["char_count_normalized"] = len(ctx.source)


# ---------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix-offsets", action="store_true")
    ap.add_argument("--output", default=str(OUTPUT))
    args = ap.parse_args()

    ctx = Context(source=normalize(SOURCE.read_text(encoding="utf-8")),
                  doc=json.loads(Path(args.output).read_text()))

    if args.fix_offsets:
        fix_offsets(ctx)

    for rule in RULES:
        rule(ctx)

    errors = [f for f in ctx.flags if f.severity == "error"]
    warns = [f for f in ctx.flags if f.severity == "warn"]
    total, grounded = ctx.counts["total"], ctx.counts["grounded"]
    ctx.doc["validation"]["metrics"] = {
        "fields_total": total,
        "fields_grounded": grounded,
        "fields_null": ctx.counts["null"],
        "groundedness_rate": round(grounded / max(total - ctx.counts["null"], 1), 4),
    }
    ctx.doc["validation"]["flags"] = [
        {"rule": f.rule, "severity": f.severity, "message": f.message, "path": f.path}
        for f in ctx.flags
    ]
    ctx.doc["validation"]["passed"] = not errors
    ctx.doc["validation"]["needs_review"] = bool(
        warns or any(p.get("needs_review") for p in ctx.doc["packages"]))

    if args.fix_offsets:
        Path(args.output).write_text(json.dumps(ctx.doc, indent=2, ensure_ascii=False) + "\n")

    print(f"fields checked : {total}  grounded: {grounded}  null: {ctx.counts['null']}")
    print(f"groundedness   : {ctx.doc['validation']['metrics']['groundedness_rate']:.2%}")
    print(f"errors         : {len(errors)}")
    print(f"warnings       : {len(warns)}")
    for f in ctx.flags:
        if f.severity != "info":
            print(f"  [{f.severity}] {f.rule}: {f.message} ({f.path})")
    print("RESULT:", "PASS" if not errors else "FAIL",
          "| needs_review:", ctx.doc["validation"]["needs_review"])
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
