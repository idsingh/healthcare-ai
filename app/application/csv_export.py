"""CSV projection.

The column set is fixed by the customer's sample output; anything the guide does
not state is written as the configured placeholder ("-" per the brief) rather
than left blank, so a consumer can tell "not stated" from "empty string".
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Iterable, Sequence

from app.domain.models import BenefitRow

COLUMNS: tuple[str, ...] = (
    "Benefit Group",
    "Dental Code",
    "Description",
    "Frequency/Limitations",
    "In-Network Coverage",
    "Out-of-Network Coverage",
)
MISSING = "-"


def clean(value: str | None, missing: str = MISSING) -> str:
    text = re.sub(r"\s+", " ", (value or "")).strip(" .;|")
    return text or missing


def to_record(row: BenefitRow, missing: str = MISSING) -> dict[str, str]:
    return {
        "Benefit Group": clean(row.benefit_group, missing),
        "Dental Code": clean(row.dental_code, missing),
        "Description": clean(row.description, missing),
        "Frequency/Limitations": clean(row.frequency, missing),
        "In-Network Coverage": clean(row.in_network, missing),
        "Out-of-Network Coverage": clean(row.out_network, missing),
    }


def write_csv(rows: Iterable[BenefitRow], path: str | Path, *, missing: str = MISSING,
              columns: Sequence[str] = COLUMNS) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow(to_record(row, missing))
            count += 1
    return count


def to_csv_string(rows: Iterable[BenefitRow], *, missing: str = MISSING,
                  columns: Sequence[str] = COLUMNS) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns))
    writer.writeheader()
    for row in rows:
        writer.writerow(to_record(row, missing))
    return buffer.getvalue()
