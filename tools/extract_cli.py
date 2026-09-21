#!/usr/bin/env python3
"""Run the pipeline over a text file without starting the API.

    python -m tools.extract_cli extracted_text.txt -o service_output.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.deps import build_llm                       # noqa: E402
from app.application.pipeline import ExtractionPipeline  # noqa: E402
from app.config import get_settings                      # noqa: E402
from app.logging_setup import configure_logging          # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    ap.add_argument("-o", "--output", default="service_output.json")
    args = ap.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    pipeline = ExtractionPipeline(settings, build_llm(settings))
    result = await pipeline.run(Path(args.path).read_text(), run_id="cli", idempotency_key="cli")

    Path(args.output).write_text(json.dumps(result.model_dump(mode="json"), indent=2) + "\n")
    metrics = result.validation.metrics
    print(f"\nwrote {args.output}: {len(result.packages)} packages, "
          f"{metrics.fields_grounded}/{metrics.fields_total - metrics.fields_null} grounded, "
          f"passed={result.validation.passed} needs_review={result.validation.needs_review}")
    return 0 if result.validation.passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
