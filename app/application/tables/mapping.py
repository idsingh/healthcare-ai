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
    (Field.out_network, ("out of network", "out of plan", "outofnetwork", "non participating",
                         "nonparticipating", "non par", "out network")),
    # No generic 'network coverage' here: it is longer than 'out of network' and would
    # win the longest-phrase contest for the out-of-network column. A bare
    # 'Network coverage' column still reaches in-network through COVERAGE_HINTS.
    (Field.in_network, ("in network", "in plan", "innetwork", "participating", "par provider",
                        "preferred provider")),
    # description before code: 'Code Description' contains both words and means description
    (Field.description, ("description", "code description", "description of benefits",
                         "benefit description", "service description", "nomenclature",
                         "benefit", "service", "procedure", "what it covers", "covers")),
    (Field.code, ("code", "codes", "ada code", "cdt code", "procedure code", "dental code",
                  "code number")),
    (Field.frequency, ("frequency", "limitation", "limitations", "periodicity", "how often",
                       "frequency limitation", "limits", "benefit limitations")),
    (Field.group, ("service category", "benefit group", "benefit category", "category", "class",
                   "service type", "benefit type")),
]

# 'benefit' is deliberately absent: a column labelled just 'Benefit' is the service,
# not a coverage amount. It is listed as a description synonym instead.
COVERAGE_HINTS = ("coverage", "plan pays", "you pay", "member pays", "copay", "coinsurance",
                  "cost share", "you owe")


def normalize(label: str) -> str:
    """Lowercase, and treat '/' and '-' as spaces so 'Out-of-network',
    'Non-Participating' and 'Frequency/limitations' match plain-word rules."""
    text = re.sub(r"[^a-z0-9/ -]", " ", (label or "").lower())
    return re.sub(r"\s+", " ", text.replace("/", " ").replace("-", " ")).strip()


def match_label(label: str) -> Field | None:
    """Deterministic mapping by longest matching phrase.

    Longest wins so that a label containing two vocabularies resolves to the more
    specific one: 'Code Description' is a description, 'Benefit Limitations' is a
    frequency, 'Non-Participating Provider' is out-of-network. Rule order only
    breaks ties.
    """
    norm = normalize(label)
    if not norm:
        return None
    best: tuple[int, int, Field] | None = None
    for order, (field, needles) in enumerate(RULES):
        for needle in needles:
            if needle in norm:
                candidate = (len(needle), -order, field)
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
    if best:
        return best[2]
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


# --------------------------------------------------------------------------
# Content-based inference: what to do when the header is missing, or worded in
# a way no synonym list anticipated. Columns are identified by what they hold.
# --------------------------------------------------------------------------

CODE_CELL = re.compile(r"^[A-Z]\d{4}[A-Z]?$")
MONEY_OR_PERCENT = re.compile(r"^\$?\s?\d[\d,]*(?:\.\d{1,2})?\s?%?(?:\s*(?:copay|coinsurance))?$", re.I)
FREQUENCY_WORDS = ("per ", "every", "each ", "year", "month", "visit", "unlimited", "once",
                   " of (", "lifetime", "as needed", "not within", "day")
NOT_COVERED = ("not covered", "no charge", "covered in full", "n/a")
SAMPLE_ROWS = 40
SHARE = 0.6


def _share(values: list[str], predicate) -> float:
    filled = [v for v in values if v and v.strip()]
    return (sum(1 for v in filled if predicate(v)) / len(filled)) if filled else 0.0


def _prune_contradictions(known: dict[Field, int], columns: list[list[str]]) -> dict[Field, int]:
    """Drop a header-derived mapping the cells contradict.

    A header can be misleading — a column headed 'Procedure' that holds D-codes is
    the code column, not the description. Content beats wording.
    """
    def col(index: int) -> list[str]:
        return columns[index] if 0 <= index < len(columns) else []

    pruned = dict(known)
    code_like = lambda v: bool(CODE_CELL.match(v.split()[0] if v.split() else ""))
    if Field.code in pruned and _share(col(pruned[Field.code]), code_like) < 0.4:
        del pruned[Field.code]
    if Field.description in pruned and _share(col(pruned[Field.description]), code_like) >= SHARE:
        del pruned[Field.description]
    for field in (Field.in_network, Field.out_network):
        if field in pruned and _share(col(pruned[field]), code_like) >= SHARE:
            del pruned[field]
    return pruned


def infer_roles(rows: list[list[str]], known: dict[Field, int] | None = None) -> dict[Field, int]:
    """Identify columns from their contents. Used for fields the header did not
    give us, so a guide with no header row — or an unheard-of header wording —
    still produces a usable table instead of empty cells."""
    known = dict(known or {})
    if not rows:
        return known
    width = max(len(r) for r in rows)
    columns = [[r[i] if i < len(r) else "" for r in rows[:SAMPLE_ROWS]] for i in range(width)]
    known = _prune_contradictions(known, columns)
    taken = set(known.values())

    def claim(field: Field, index: int | None) -> None:
        if field not in known and index is not None and index not in taken:
            known[field] = index
            taken.add(index)

    if Field.code not in known:
        scores = [(_share(col, lambda v: bool(CODE_CELL.match(v.split()[0] if v.split() else ""))), i)
                  for i, col in enumerate(columns)]
        best = max(scores, default=(0, None))
        claim(Field.code, best[1] if best[0] >= SHARE else None)

    money = [i for i, col in enumerate(columns)
             if i not in taken and _share(col, lambda v: bool(MONEY_OR_PERCENT.match(v.strip()))
                                          or v.strip().lower() in NOT_COVERED) >= SHARE]
    if Field.in_network not in known and money:
        claim(Field.in_network, money[0])
    if Field.out_network not in known and len(money) > 1:
        claim(Field.out_network, money[1])

    if Field.frequency not in known:
        scores = [(_share(col, lambda v: any(w in v.lower() for w in FREQUENCY_WORDS)), i)
                  for i, col in enumerate(columns) if i not in taken]
        best = max(scores, default=(0, None))
        claim(Field.frequency, best[1] if best[0] >= 0.4 else None)

    if Field.description not in known:
        lengths = [(sum(len(v) for v in col) / max(len(col), 1), i)
                   for i, col in enumerate(columns) if i not in taken]
        best = max(lengths, default=(0, None))
        claim(Field.description, best[1] if best[0] >= 8 else None)

    return known
