"""Header label -> canonical field.

Deterministic synonym matching first (covers the labels these documents actually
use and the obvious variants), LLM fallback for headers we have never seen. This
is what keeps the extractor generic: nothing anywhere keys off a plan name, a
file name or a fixed column order.
"""
from __future__ import annotations

import re
from enum import Enum


class Field(str, Enum):
    code = "code"
    description = "description"
    frequency = "frequency"
    in_network = "in_network"
    out_network = "out_network"
    group = "group"
    other = "other"


# Ordered: the first rule that matches a normalized label wins.
RULES: list[tuple[Field, tuple[str, ...]]] = [
    (Field.out_network, ("out of network", "out-of-network", "outofnetwork", "non participating",
                         "nonparticipating", "non par", "out network")),
    (Field.in_network, ("in network", "in-network", "innetwork", "participating", "par provider",
                        "preferred provider", "network coverage")),
    # description before code: 'Code Description' contains both words and means description
    (Field.description, ("description", "code description", "description of benefits",
                         "benefit description", "service description", "nomenclature")),
    (Field.code, ("code", "codes", "ada code", "cdt code", "procedure code", "dental code",
                  "code number")),
    (Field.frequency, ("frequency", "limitation", "limitations", "periodicity", "frequency limitation",
                       "frequency/limitations", "limits", "benefit limitations")),
    (Field.group, ("service category", "benefit group", "benefit category", "category", "class",
                   "service type", "benefit type")),
]

COVERAGE_HINTS = ("coverage", "plan pays", "you pay", "member pays", "copay", "coinsurance",
                  "cost share", "benefit")


def normalize(label: str) -> str:
    """Lowercase, and treat '/' and '-' as spaces so 'Out-of-network',
    'Non-Participating' and 'Frequency/limitations' match plain-word rules."""
    text = re.sub(r"[^a-z0-9/ -]", " ", (label or "").lower())
    return re.sub(r"\s+", " ", text.replace("/", " ").replace("-", " ")).strip()


def match_label(label: str) -> Field | None:
    """Deterministic mapping. Returns None when nothing matches confidently."""
    norm = normalize(label)
    if not norm:
        return None
    for field, needles in RULES:
        for needle in needles:
            if needle in norm:
                return field
    if any(hint in norm for hint in COVERAGE_HINTS):
        return Field.in_network          # unqualified coverage column; flagged by the caller
    return None


def map_labels(labels: list[str]) -> dict[Field, int]:
    """Label list -> {canonical field: column index}. Duplicate mappings keep the
    first column, which is what a repeated 'coverage' header means in practice."""
    mapping: dict[Field, int] = {}
    for index, label in enumerate(labels):
        field = match_label(label)
        if field and field not in mapping:
            mapping[field] = index
    return mapping


def unqualified_coverage(labels: list[str]) -> bool:
    """True when a coverage column exists but never says which network it is."""
    norms = [normalize(x) for x in labels]
    has_coverage = any(any(h in n for h in COVERAGE_HINTS) for n in norms)
    qualified = any("network" in n or "participating" in n for n in norms)
    return has_coverage and not qualified


def unmapped(labels: list[str]) -> list[str]:
    return [x for x in labels if x.strip() and match_label(x) is None]
