"""The use case: text in, validated ExtractionResult out.

    preprocess -> segment -> (scan | llm) -> merge -> validate

Package blocks are independent, so they run concurrently under a semaphore.
A block whose LLM pass fails degrades to deterministic-only output; the
document still completes, flagged.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.application.merge import PackageAssembler
from app.application.preprocess import NormalizedDocument, Preprocessor, collapse
from app.application.scanners import Candidate, scan_block
from app.application.segmentation import Block, Segmenter
from app.application.validation import ValidationPipeline
from app.application.llm_extractor import BlockExtractor, LlmStats
from app.config import PIPELINE_VERSION, PROMPT_VERSION, Settings
from app.domain.contracts import LlmPackageExtraction
from app.domain.errors import BlockExtractionFailed
from app.domain.models import (
    DocumentInfo, Evidence, ExtractionResult, Field, FieldStatus, Flag, Package, RunInfo, Severity,
    Source, utcnow,
)
from app.domain.ports import LLMClient
from app.logging_setup import get_logger

log = get_logger("extract.pipeline")
SENTENCE_END = (".", "!", "?", ":", ";")


@dataclass
class _BlockOutcome:
    block: Block
    candidates: list[Candidate]
    extraction: LlmPackageExtraction | None
    stats: LlmStats


class ExtractionPipeline:
    def __init__(self, settings: Settings, llm: LLMClient, *,
                 preprocessor: Preprocessor | None = None,
                 segmenter: Segmenter | None = None,
                 validators: ValidationPipeline | None = None):
        self._s = settings
        self._llm = llm
        self._pre = preprocessor or Preprocessor(settings)
        self._seg = segmenter or Segmenter()
        self._validators = validators or ValidationPipeline()
        self._extractor = BlockExtractor(llm, settings)

    async def run(self, text: str, *, run_id: str, idempotency_key: str) -> ExtractionResult:
        started = utcnow()
        t0 = time.perf_counter()

        doc = self._pre.run(text)
        blocks = self._seg.segment(doc)
        package_blocks = [b for b in blocks if b.kind == "package"]
        header_block = next((b for b in blocks if b.kind == "header"), None)
        log.info("document segmented", extra={
            "pages": doc.stats["pages"], "packages": len(package_blocks), **doc.stats})

        outcomes = await self._process_blocks(package_blocks)
        stats = LlmStats()
        for o in outcomes:
            stats.merge(o.stats)

        assembler = PackageAssembler(doc)
        packages = [assembler.assemble(o.block, o.candidates, o.extraction) for o in outcomes]

        result = ExtractionResult(
            run=RunInfo(
                run_id=run_id, content_sha256=doc.content_sha256, idempotency_key=idempotency_key,
                started_at=started, model_id=getattr(self._llm, "model_id", "unknown"),
                model_params={"temperature": self._s.temperature, "seed": self._s.seed},
                prompt_version=PROMPT_VERSION, pipeline_version=PIPELINE_VERSION,
                blocks_total=len(package_blocks), blocks_degraded=len(stats.blocks_degraded),
                llm_calls=stats.calls, llm_retries=stats.retries,
                degraded=bool(stats.blocks_degraded),
                partial_reason="llm_block_failure" if stats.blocks_degraded else None),
            document=self._document_info(doc, header_block, assembler),
            packages=packages,
        )
        result = self._validators.run(result, doc)
        if not package_blocks:
            result.validation.flags.append(Flag(
                rule="segmentation.no_packages", severity=Severity.warn,
                message="no supplemental package headings were found in this document",
                path="/packages"))
            result.validation.needs_review = True
        result.run.completed_at = utcnow()

        log.info("extraction complete", extra={
            "run_id": run_id, "packages": len(packages), "llm_calls": stats.calls,
            "llm_retries": stats.retries, "blocks_degraded": len(stats.blocks_degraded),
            "passed": result.validation.passed, "needs_review": result.validation.needs_review,
            "groundedness_rate": result.validation.metrics.groundedness_rate,
            "duration_ms": round((time.perf_counter() - t0) * 1000)})
        return result

    async def _process_blocks(self, blocks: list[Block]) -> list[_BlockOutcome]:
        semaphore = asyncio.Semaphore(self._s.block_concurrency)

        async def process(block: Block) -> _BlockOutcome:
            async with semaphore:
                candidates = scan_block(block)
                try:
                    extraction, stats = await self._extractor.extract(block, candidates)
                except BlockExtractionFailed as exc:
                    log.error("block degraded to deterministic-only", extra={
                        "block_id": block.block_id, "error_code": exc.code, "reason": exc.message})
                    stats = LlmStats(failures=1, blocks_degraded=[block.block_id])
                    return _BlockOutcome(block, candidates, None, stats)
                return _BlockOutcome(block, candidates, extraction, stats)

        return list(await asyncio.gather(*(process(b) for b in blocks)))

    def _document_info(self, doc: NormalizedDocument, header: Block | None,
                       assembler: PackageAssembler) -> DocumentInfo:
        info = DocumentInfo(
            source_pages=[p for p in doc.page_numbers if p is not None],
            truncated=not doc.flat.rstrip().endswith(SENTENCE_END),
            char_count_normalized=len(doc.flat))
        if header is None:
            return info
        for c in scan_block(header):
            ev = assembler.evidence(c.quote, header)
            if not ev:
                continue
            field = {"plan_name": Field[str], "plan_year": Field[int],
                     "document_type": Field[str], "form_id": Field[str]}.get(c.kind)
            if field is None:
                continue
            setattr(info, c.kind, field(value=c.value, status=FieldStatus.found,
                                        confidence=c.confidence, source=Source.deterministic,
                                        evidence=ev))
        return info
