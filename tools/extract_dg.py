#!/usr/bin/env python3
"""Dental Guide -> CSV.

    python -m tools.extract_dg data/dental_guides/*.pdf
    python -m tools.extract_dg data/dental_guides -o output/dental_guides

Writes one CSV per guide plus a combined CSV, and a JSON run report holding the
detected columns, per-page strategies and validation flags for each document.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.deps import build_cascade, build_llm            # noqa: E402
from app.application.csv_export import COLUMNS, write_csv   # noqa: E402
from app.application.dental_guide import DentalGuidePipeline  # noqa: E402
from app.config import get_settings                         # noqa: E402
from app.logging_setup import configure_logging             # noqa: E402


def collect(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        files.extend(sorted(path.glob("*.pdf")) if path.is_dir() else [path])
    return [f for f in files if f.suffix.lower() == ".pdf"]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="PDF files or a directory of them")
    ap.add_argument("-o", "--out-dir", default="output/dental_guides")
    ap.add_argument("--missing", default="-", help="placeholder for values the guide does not state")
    ap.add_argument("--no-llm", action="store_true", help="skip model-assisted benefit-group naming")
    args = ap.parse_args()

    settings = get_settings()
    if args.no_llm:
        settings = settings.model_copy(update={"dg_llm_grouping": False})
    configure_logging(settings.log_level, json_logs=False)

    files = collect(args.paths)
    if not files:
        print("no PDF files found", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    pipeline = DentalGuidePipeline(settings, build_llm(settings), cascade=build_cascade(settings))
    combined, report, failed = [], [], 0

    for path in files:
        try:
            result = await pipeline.run(path)
        except Exception as exc:                           # one bad file must not stop the batch
            failed += 1
            print(f"  {path.name}: FAILED ({exc})", file=sys.stderr)
            report.append({"file": path.name, "error": str(exc)})
            continue
        target = out_dir / f"{path.stem}.csv"
        write_csv(result.rows, target, missing=args.missing)
        combined.extend(result.rows)
        report.append({
            "file": path.name, "rows": len(result.rows), "pages": result.document.pages,
            "pages_with_rows": result.document.pages_with_rows,
            "columns_detected": result.document.column_labels,
            "columns_mapped": result.document.mapped_fields,
            "columns_unmapped": result.document.unmapped_columns,
            "strategies": result.document.strategies,
            "flags": [f.model_dump(mode="json") for f in result.validation.flags],
            "csv": str(target),
        })
        print(f"  {path.name:<46} {len(result.rows):>4} rows -> {target}")

    if combined:
        all_csv = out_dir / "all_guides.csv"
        write_csv(combined, all_csv, missing=args.missing)
        print(f"\ncombined: {len(combined)} rows -> {all_csv}  (columns: {', '.join(COLUMNS)})")
    (out_dir / "extraction_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"report  : {out_dir / 'extraction_report.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
